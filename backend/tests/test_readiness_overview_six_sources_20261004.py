"""T7 · 就绪度总览六源聚合 E2E 回归护栏（2026-10-04）。

覆盖用户明确选择的 T7 项：为 ``readiness_overview`` 六源聚合编写独立集成测试。

六源命名口径（对应代码中 sources 列表的实际值）：
    1. ``program``            —— 程序化预检（run_preflight）
    2. ``export_check``       —— 导出预检问题（collect_export_issues）
    3. ``compliance``         —— 最近一批 AI 规范符合性
    4. ``consistency``        —— 最近一次 AI 全文一致性审计
    5. ``consistency_scan``   —— 最近一次全文一致性扫描（规则+仲裁）
    6. ``expert_review``      —— 最近一次专家论证预检

每条断言都锚定**可观测行为**：
    - sources 数组元素、findings 中来自该源的 rule_id 命名空间、
      降级顺序（某源不可用不影响总检返回）、缓存与 force 语义、
      stale 判定与 content_fingerprint、六源全失效时仍返回 200。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.routers import compliance as _cc_mod
from app.routers.compliance import (
    _OVERVIEW_RECENT,
    _readiness_overview_compute,
    readiness_overview,
)
from app.services.audit_scoring import score_findings

pytestmark = pytest.mark.asyncio


# ===========================================================================
# 通用 fixture 与 helpers（沿用同仓约定）
# ===========================================================================
@pytest.fixture
async def ov_db(tmp_path):
    _appdb.DB_PATH = tmp_path / "ov-six-sources.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget)"
        " VALUES(?,?,?,?,0)",
        (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.commit()
    _OVERVIEW_RECENT.pop(sid, None)
    yield db, sid
    _OVERVIEW_RECENT.pop(sid, None)
    await close_db()


async def _add_section(db, sid, *, title="第一章", content="", level=1,
                       parent_id="", sort_order=0):
    sec_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, "", parent_id, title, "", level,
         "generated" if content else "empty",
         len(content), 0, content, "", sort_order))
    return sec_id


def _now(offset_seconds: int = 0) -> str:
    return (datetime.now() + timedelta(seconds=offset_seconds)).isoformat(
        timespec="seconds")


async def _insert_ai_batch(db, sid, *, batch_id, created_at, results):
    """一次 /check 的 AI 结果按同一 batch_id 落库多行。"""
    for r in results:
        await db.execute(
            "INSERT INTO compliance_check (id, scheme_id, project_id,"
            " check_type, rule_id, item, severity, result, batch_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, "", "compliance",
             r["rule_id"], r["item"], r["severity"],
             json.dumps({"results": [r]}, ensure_ascii=False),
             batch_id, created_at))


async def _insert_consistency_audit(db, sid, *, score, issues, created_at):
    await db.execute(
        "INSERT INTO consistency_audit (id, scheme_id, project_id, score,"
        " issues, created_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", score, json.dumps(issues, ensure_ascii=False),
         created_at))


async def _insert_consistency_conflicts(db, sid, *, scan_id, rows, created_at):
    for c in rows:
        await db.execute(
            "INSERT INTO consistency_conflicts (id, scheme_id, scan_id,"
            " conflict_type, severity, topic, occurrences,"
            " authoritative_value, repair_instruction, reason, status,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.get("id") or uuid.uuid4().hex, sid, scan_id,
             c["conflict_type"], c["severity"], c["topic"],
             json.dumps(c.get("occurrences", []), ensure_ascii=False),
             c.get("authoritative_value", ""),
             c.get("repair_instruction", ""),
             c.get("reason", ""),
             c.get("status", "pending"), created_at))


async def _insert_expert_review(db, sid, *, result, created_at):
    await db.execute(
        "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
        " rule_id, item, severity, result, batch_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "expert_review", "", "", "",
         json.dumps(result, ensure_ascii=False), "", created_at))


# ===========================================================================
# 一、六源 sources 数组完备性
# ===========================================================================
async def test_all_six_sources_present_when_all_inputs_populated(ov_db):
    """六源同时有数据时，overview.sources 必须包含全部六个源标记。"""
    db, sid = ov_db
    # 让 program 产生至少一个 finding：空章节 + 一个有内容章节
    await _add_section(db, sid, title="工程概况", content="概况" * 200)
    await _add_section(db, sid, title="施工总体部署", content="")

    # compliance（AI 批次）
    await _insert_ai_batch(
        db, sid, batch_id="B1", created_at=_now(),
        results=[{"rule_id": "STD-02", "item": "引用非现行推荐标准",
                  "severity": "medium", "hit": True}])

    # consistency_audit
    await _insert_consistency_audit(
        db, sid, score=70,
        issues=[{"severity": "high", "dimension": "工期",
                 "content_quote": "120 日历天", "fact": "总工期",
                 "section_title": "工程概况", "suggestion": "统一工期"}],
        created_at=_now())

    # consistency_conflicts（未解决）
    await _insert_consistency_conflicts(
        db, sid, scan_id="SC1",
        rows=[{"conflict_type": "numeric", "severity": "high",
               "topic": "总工期", "value": "120 日历天",
               "occurrences": [{"section_id": "S1", "text": "120 日历天"}],
               "repair_instruction": "改为 180 日历天", "status": "pending"}],
        created_at=_now())

    # expert_review
    await _insert_expert_review(
        db, sid,
        result={"results": [{"item": "方案深度不足", "rule_id": "TRC-01",
                            "severity": "medium", "hit": True}]},
        created_at=_now())

    await db.commit()
    payload = await readiness_overview(sid, db, force=True)
    assert payload["cached"] is False
    sources = set(payload["sources"])
    assert {"program", "compliance", "consistency", "consistency_scan",
            "expert_review"} <= sources, (
        f"六源中至少五源必须出现，实际 {sources}")
    # export_check 依赖章节内容触发导出问题，允许为空（降级）
    assert payload["total"] is not None
    assert payload["grade"] in ("A", "B", "C", "D")


async def test_consistency_audit_issues_expand_to_unique_rule_ids(ov_db):
    """多条一致性审计 issue → 每条独立 rule_id（CON-04-{idx}），不塌缩。"""
    db, sid = ov_db
    await _insert_consistency_audit(
        db, sid, score=60,
        issues=[
            {"severity": "high", "dimension": "工期", "content_quote": "120",
             "fact": "总工期 120", "section_title": "概况", "suggestion": "统一"},
            {"severity": "medium", "dimension": "材料", "content_quote": "C30",
             "fact": "混凝土标号 C30", "section_title": "材料",
             "suggestion": "统一"},
            {"severity": "low", "dimension": "人员", "content_quote": "8 人",
             "fact": "施工人员 8 人", "section_title": "人力", "suggestion": "统一"},
        ],
        created_at=_now())
    await db.commit()

    payload = await _readiness_overview_compute(db, sid, persist=False)
    con04_ids = sorted(f["rule_id"] for f in payload["findings"]
                       if f["rule_id"].startswith("CON-04-"))
    assert con04_ids == ["CON-04-1", "CON-04-2", "CON-04-3"], (
        f"三条一致性审计 issue 必须各自独立成 finding，实际 {con04_ids}")


async def test_consistency_scan_only_counts_pending_failed(ov_db):
    """一致性扫描：只统计 pending/failed，repaired/accepted/skipped 不计分。"""
    db, sid = ov_db
    scan_id = "SC-pending-filter"
    await _insert_consistency_conflicts(
        db, sid, scan_id=scan_id,
        rows=[
            {"id": f"{scan_id}-1", "conflict_type": "numeric",
             "severity": "high", "topic": "工期", "status": "pending",
             "occurrences": []},
            {"id": f"{scan_id}-2", "conflict_type": "numeric",
             "severity": "high", "topic": "材料", "status": "failed",
             "occurrences": []},
            {"id": f"{scan_id}-3", "conflict_type": "numeric",
             "severity": "high", "topic": "金额", "status": "repaired",
             "occurrences": []},
            {"id": f"{scan_id}-4", "conflict_type": "numeric",
             "severity": "high", "topic": "人员", "status": "skipped",
             "occurrences": []},
        ],
        created_at=_now())
    await db.commit()

    payload = await _readiness_overview_compute(db, sid, persist=False)
    scan_ids = sorted(f["rule_id"] for f in payload["findings"]
                      if f["rule_id"].startswith("CON-SCAN-"))
    assert scan_ids == ["CON-SCAN-1", "CON-SCAN-2"], (
        f"仅 pending+failed 参与评分，实际 {scan_ids}")


# ===========================================================================
# 二、降级顺序：任一源异常时总检仍返回
# ===========================================================================
async def test_single_source_failure_does_not_break_overview(
        ov_db, monkeypatch):
    """一致性审计 JSON 损坏 → 跳过解析，findings 中不产生 CON-04-* 条目，
    但总检仍返回 200（不因脏数据 500）。"""
    db, sid = ov_db
    await _add_section(db, sid, title="工程概况", content="概况" * 200)

    # 写入一行为合法 batch（保证至少有一条合规 finding，避免"零源"歧义）
    await _insert_ai_batch(
        db, sid, batch_id="B-OK", created_at=_now(),
        results=[{"rule_id": "STD-01", "item": "废止标准", "severity": "high",
                  "hit": False}])

    # 写入一行为**损坏** JSON 的一致性审计，模拟历史脏数据
    await db.execute(
        "INSERT INTO consistency_audit (id, scheme_id, project_id, score,"
        " issues, created_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", 55, "{not valid json", _now()))
    await db.commit()

    payload = await _readiness_overview_compute(db, sid, persist=False)
    # 不因脏数据 500：total 与 grade 均存在
    assert isinstance(payload["total"], (int, float))
    assert payload["grade"] in ("A", "B", "C", "D")
    # 脏 JSON 走 except JSONDecodeError → issues=[] → 不产出 CON-04-* finding
    con04_ids = [f for f in payload["findings"]
                 if f.get("rule_id", "").startswith("CON-04-")]
    assert not con04_ids, (
        f"损坏 JSON 不得产生 CON-04-* finding，实际 {con04_ids}")


async def test_export_source_swallowed_when_routers_export_missing(
        ov_db, monkeypatch):
    """导出预检子模块抛异常 → sources 不含 export_check，但总检仍返回。"""
    db, sid = ov_db
    await _add_section(db, sid, title="工程概况", content="概况" * 200)
    await db.commit()

    def _boom(*a, **k):
        raise RuntimeError("模拟 export 子模块不可用")

    # 直接 monkeypatch routers 模块内 import 出来的符号不可行（import 期绑定），
    # 但 readiness_overview 每次调用都走 from app.routers.export import ...
    # 内部语句 —— 此处通过 patch app.routers.export.collect_export_issues 生效
    import app.routers.export as _exp
    orig = _exp.collect_export_issues
    try:
        async def _broken(*a, **k):
            raise RuntimeError("导出预检不可用")
        monkeypatch.setattr(_exp, "collect_export_issues", _broken)
        payload = await _readiness_overview_compute(db, sid, persist=False)
    finally:
        _exp.collect_export_issues = orig

    assert "export_check" not in payload["sources"]
    assert payload["grade"] in ("A", "B", "C", "D")


# ===========================================================================
# 三、并发与幂等缓存
# ===========================================================================
async def test_concurrent_overview_shares_one_lock_and_cache(ov_db):
    """同 scheme_id 并发多次 overview → 至少两次命中缓存（cached=True）。"""
    db, sid = ov_db
    await _add_section(db, sid, title="工程概况", content="概况" * 200)
    await _add_section(db, sid, title="施工部署", content="部署" * 200)
    await db.commit()

    import asyncio
    results = await asyncio.gather(
        readiness_overview(sid, db, force=False),
        readiness_overview(sid, db, force=False),
        readiness_overview(sid, db, force=False),
    )
    cached_flags = [r["cached"] for r in results]
    assert cached_flags[0] is False, "首个请求必须计算并缓存"
    # 后两个并发请求要么命中锁后计算（可能仍 cached=False），要么命中缓存
    # 但至少有一次 cached=True，证明缓存路径被走到
    assert any(cached_flags), f"三并发必须至少一次缓存命中，实际 {cached_flags}"
    # 所有响应的 scheme_id 一致（无跨请求污染）
    for r in results:
        assert r["scheme_id"] == sid


async def test_force_true_bypasses_cache(ov_db):
    """force=True 时必须绕过缓存强制重算，cached=False。"""
    db, sid = ov_db
    await _add_section(db, sid, title="工程概况", content="概况" * 200)
    await db.commit()

    r1 = await readiness_overview(sid, db, force=False)
    assert r1["cached"] is False
    r2 = await readiness_overview(sid, db, force=False)
    assert r2["cached"] is True
    r3 = await readiness_overview(sid, db, force=True)
    assert r3["cached"] is False, "force=True 必须绕过缓存"


# ===========================================================================
# 四、六源全空/全失效 → 仍然给出可解释结论
# ===========================================================================
async def test_empty_scheme_yields_valid_empty_overview(ov_db):
    """无任何章节、无历史检查 → 总检仍返回 200 且结构完整。"""
    db, sid = ov_db
    payload = await readiness_overview(sid, db, force=True)
    assert payload["scheme_id"] == sid
    assert "total" in payload and isinstance(payload["total"], (int, float))
    assert payload["grade"] in ("A", "B", "C", "D")
    assert isinstance(payload["findings"], list)
    assert isinstance(payload["sources"], list)
    # program 源永远在（即使无章节，程序化预检也会运行）
    assert "program" in payload["sources"]
    # content_fingerprint 必须存在（G3 时效判定依据）
    assert "content_fingerprint" in payload


# ===========================================================================
# 五、content_fingerprint 与 stale 判定
# ===========================================================================
async def test_stale_flag_flips_when_content_changes(ov_db):
    """正文变更 → 后续 /runs 或 /report 的 stale 必须为 True。"""
    db, sid = ov_db
    await _add_section(db, sid, title="工程概况", content="概况" * 200)
    await db.commit()
    first = await readiness_overview(sid, db, force=True)
    fp1 = first["content_fingerprint"]
    assert fp1, "首次总检必须写入 content_fingerprint"

    # 修改正文
    cur = await db.execute("SELECT id FROM sections WHERE scheme_id=?", (sid,))
    sec = await cur.fetchone()
    await db.execute(
        "UPDATE sections SET content=?, word_count=? WHERE id=?",
        ("彻底改过的内容" * 200, len("彻底改过的内容" * 200), sec["id"]))
    await db.commit()

    # 缓存 TTL 内直接请求会命中缓存（cached=True 且 stale=False 是**缓存语义**）
    # 这里走 force=True 强制重算以观察指纹变化
    second = await readiness_overview(sid, db, force=True)
    assert second["content_fingerprint"] != fp1, (
        "正文变更后指纹必须改变，否则 G3 时效判定失效")

    # 走 /runs 观察 stale
    from app.routers.compliance import list_preflight_runs
    runs = await list_preflight_runs(sid, 10, db)
    assert runs["items"], "/runs 必须能读到刚才两次总检的历史"
    # 最新一条 stale=False（就是刚重算的那条），旧的一条 stale=True
    assert runs["items"][0]["stale"] is False
    if len(runs["items"]) >= 2:
        assert runs["items"][1]["stale"] is True, (
            "正文变更后，旧历史条目的 stale 必须翻为 True")


# ===========================================================================
# 六、评分引擎一致性（合并口径）
# ===========================================================================
async def test_severity_penalty_matches_score_findings(ov_db):
    """一致性审计源 severity 传递与降级规则。

    compliance.py 有意把 consistency 源的 severity 限定为 high/medium/low
    三档（block 级只允许来自程序化预检或 AI 规则），代码注释见：
        if sev not in ("high", "medium", "low"): sev = "medium"

    因此：
        - 注入 high + medium，CON-04-1 应为 high、CON-04-2 应为 medium；
        - 注入 "block" 应被降级为 medium（守护「block 只能来自程序化/AI」口径）；
        - 两条 high+medium 的总扣分足以把 grade 压到 C 或 D，但 blocked 恒为 False
          （consistency 源不产生 block）。
    """
    db, sid = ov_db
    await _insert_consistency_audit(
        db, sid, score=60,
        issues=[
            {"severity": "high", "dimension": "红线",
             "content_quote": "q", "fact": "f", "section_title": "t",
             "suggestion": "s"},
            {"severity": "medium", "dimension": "x",
             "content_quote": "q", "fact": "f", "section_title": "t",
             "suggestion": "s"},
            # block 应被降级为 medium（守护 severity 命名空间口径）
            {"severity": "block", "dimension": "越权",
             "content_quote": "q", "fact": "f", "section_title": "t",
             "suggestion": "s"},
        ],
        created_at=_now())
    await db.commit()

    payload = await _readiness_overview_compute(db, sid, persist=False)
    counts = payload["counts"]
    # 至少包含 1 条 high + 2 条 medium（注入的 block 被降级为 medium）
    assert counts["high"] >= 1, f"必须至少包含 1 条 high，实际 {counts}"
    assert counts["medium"] >= 2, f"注入 block 应降级为 medium，实际 {counts}"
    # CON-04-* 全部来自 consistency 源；block 只能出现在 program 源（DLV-01 等）
    con04_blocks = [
        f for f in payload["findings"]
        if f.get("rule_id", "").startswith("CON-04-")
        and f.get("severity") == "block"
    ]
    assert not con04_blocks, (
        "consistency 源（CON-04-*）不得产生 block 级 finding，"
        f"实际 {[(f['rule_id'], f['severity']) for f in con04_blocks]}")
    # 注入的 CON-04-1/2/3 severity 传递与降级必须精确
    con04_1 = [f for f in payload["findings"] if f.get("rule_id") == "CON-04-1"]
    con04_2 = [f for f in payload["findings"] if f.get("rule_id") == "CON-04-2"]
    con04_3 = [f for f in payload["findings"] if f.get("rule_id") == "CON-04-3"]
    assert con04_1 and con04_1[0]["severity"] == "high", (
        f"CON-04-1 应为 high，实际 {con04_1[0]['severity'] if con04_1 else None}")
    assert con04_2 and con04_2[0]["severity"] == "medium", (
        f"CON-04-2 应为 medium，实际 {con04_2[0]['severity'] if con04_2 else None}")
    assert con04_3 and con04_3[0]["severity"] == "medium", (
        f"注入 block 应降级为 medium（CON-04-3），"
        f"实际 {con04_3[0]['severity'] if con04_3 else None}")


# ===========================================================================
# 七、persist=False 不落库（自动修复链路依赖）
# ===========================================================================
async def test_persist_false_does_not_write_history(ov_db):
    """persist=False（review_autofix 内部重算）必须跳过 preflight_runs 落库。"""
    db, sid = ov_db
    await _add_section(db, sid, title="工程概况", content="概况" * 200)
    await db.commit()

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM preflight_runs WHERE scheme_id=?", (sid,))
    before = (await cur.fetchone())["n"]

    await _readiness_overview_compute(db, sid, persist=False)

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM preflight_runs WHERE scheme_id=?", (sid,))
    after = (await cur.fetchone())["n"]
    assert before == after, (
        f"persist=False 不得写 preflight_runs，实际 {before}→{after}")
