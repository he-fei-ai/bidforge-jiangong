"""审核与预检模块 2026-09-23 后续项（遗留清单收编）的回归锁测试。

覆盖三项根治：

1. AI 批次跨秒截头根治（compliance_check.batch_id）
   - db.py::_migrate 幂等补列（旧库无 batch_id → 重启后列存在）
   - /check 同一次调用全部行共享一个 batch_id，且响应回传批号
   - 就绪度总检（/overview）按 batch_id 取「最近一批」：
     两次 /check 同秒交错写入时，旧 rowid 锚定口径会把两批混读，
     新口径只取最新批 —— 附旧数据（batch_id 空串）回退 rowid 锚定的反例
2. CMP-09 与 TRC-01 跨维度双扣修复（规则口径互补）
   - 全部计算书章节都无计算过程 → 只报 CMP-09（block），TRC-01 抑制
   - 整体有过程、个别章节缺失 → 只报 TRC-01，CMP-09 追加条款不发
3. 孤儿 API 处置：/dimensions、/consistency-audit/*/history、
   /preflight/*、/review/statuses 保留端点但在 OpenAPI 元数据上
   标记 deprecated（大版本清理的信号锁）
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.models import ComplianceCheckIn
from app.routers import compliance as _compliance_mod
from app.routers.compliance import _PREFLIGHT_RECENT, readiness_overview
from app.services.audit_rules import get_rule
from app.services.preflight_engine import PreflightContext, run_preflight

# 注：本仓 pytest 为 asyncio auto 模式，async 用例无需显式 mark；
# 不设置模块级 pytestmark，避免纯同步用例被误挂 asyncio 警告。


@pytest.fixture
async def db_ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "review-preflight-followups.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,0)",
        (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.commit()
    _PREFLIGHT_RECENT.pop(sid, None)
    yield db, pid, sid
    _PREFLIGHT_RECENT.pop(sid, None)
    await close_db()


async def _insert_section(db, sid, title="第一章", content="", sort_order=0):
    sec_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, "", "", title, "", 1,
         "generated" if content else "empty",
         len(content), 0, content, "", sort_order))
    return sec_id


async def _insert_ai_row(db, sid, *, rule_id, severity, hit, batch_id="",
                         created_at="2026-09-23 10:00:00", item="AI 检查项"):
    """直插一行 compliance_check（check_type='compliance'），可指定批号/时间戳。"""
    await db.execute(
        "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
        " rule_id, item, severity, result, suggestion, batch_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "compliance", rule_id, item, severity,
         json.dumps({"rule_id": rule_id, "item": item, "severity": severity,
                     "hit": hit}, ensure_ascii=False),
         "", batch_id, created_at))


def _mk_ctx(sections):
    return PreflightContext(
        scheme_id="s", scheme_name="深基坑支护专项方案", scheme_type="基坑工程",
        word_budget=0, sections=sections, charts=[])


def _mk_section(title, content):
    return {
        "id": uuid.uuid4().hex, "title": title, "content": content,
        "word_count": len(content), "level": 1, "parent_id": "",
        "status": "generated" if content else "empty",
    }


# ===========================================================================
# 一、batch_id：迁移补列 / 写入口径 / 读取端批次锚定
# ===========================================================================
async def test_migrate_adds_batch_id_to_legacy_db(tmp_path):
    """旧库（表结构无 batch_id）经 init_db 幂等迁移后列必须存在且默认空串。"""
    dbfile = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(dbfile)
    con.execute(
        "CREATE TABLE compliance_check ("
        " id TEXT PRIMARY KEY, project_id TEXT DEFAULT '',"
        " scheme_id TEXT DEFAULT '', check_type TEXT DEFAULT 'compliance',"
        " rule_id TEXT DEFAULT '', item TEXT DEFAULT '',"
        " severity TEXT DEFAULT '', result TEXT DEFAULT '',"
        " suggestion TEXT DEFAULT '',"
        " created_at TEXT DEFAULT (datetime('now','localtime')))")
    con.commit()
    con.close()

    _appdb.DB_PATH = dbfile
    await init_db()
    db = await get_conn()
    cur = await db.execute("PRAGMA table_info(compliance_check)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "batch_id" in cols, "_migrate 必须为旧库幂等补 batch_id 列"
    await close_db()


async def test_check_shares_one_batch_id_across_rows(db_ctx, monkeypatch):
    """一次 /check 写入的全部行共享同一 batch_id，且响应体回传批号。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="工程概况", content="概况" * 200)
    await db.commit()

    async def _fake_collect(messages, *_a, **_k):
        return {"results": [
            {"rule_id": "STD-01", "item": "引用废止标准", "severity": "high", "hit": False},
            {"rule_id": "STD-02", "item": "引用非现行推荐标准", "severity": "low", "hit": True},
        ]}, None

    monkeypatch.setattr(_compliance_mod, "collect_json_response", _fake_collect)
    out = await _compliance_mod.compliance_check(
        ComplianceCheckIn(scheme_id=sid, rule_ids=["STD-01", "STD-02"]), db)
    batch_id = out.get("batch_id")
    assert batch_id, "/check 响应应回传 batch_id"
    cur = await db.execute(
        "SELECT batch_id, COUNT(*) AS n FROM compliance_check"
        " WHERE scheme_id=? AND check_type='compliance' GROUP BY batch_id", (sid,))
    groups = [dict(r) for r in await cur.fetchall()]
    assert len(groups) == 1 and groups[0]["batch_id"] == batch_id
    assert groups[0]["n"] == 2


async def test_overview_uses_batch_id_not_rowid_when_same_second(db_ctx):
    """两次 /check 同秒交错写入：总检只取最新一批（batch B），

    旧 rowid 锚定口径在同一 created_at 组内取 MIN(rowid) 起步读，
    会把先写的批次 A 一起混读进来（截头/混批 BUG 的根因）。
    规则号用注册表外的 AI 自由编号，避开与程序化链路同 rule_id 的
    merge 坑塌（那是既有契约行为，不在本测范围）。
    """
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="工程概况", content="概况" * 200)
    await _insert_ai_row(db, sid, rule_id="AIX-01", severity="high",
                         hit=False, batch_id="batchA", item="旧批检查项一")
    await _insert_ai_row(db, sid, rule_id="AIX-02", severity="high",
                         hit=False, batch_id="batchA", item="旧批检查项二")
    await _insert_ai_row(db, sid, rule_id="AIX-03", severity="block",
                         hit=False, batch_id="batchB", item="新批检查项")
    await db.commit()

    ov = await readiness_overview(sid, db, force=True)
    ai_rids = {f.get("rule_id") for f in ov["findings"]
               if f.get("mode") == "ai"}
    assert "AIX-03" in ai_rids, "最新一批（batchB）必须被总检消费"
    assert not ({"AIX-01", "AIX-02"} & ai_rids), \
        "旧批次（batchA）不得与最新批混读（rowid 锚定截头 BUG 的反例）"


async def test_overview_falls_back_to_rowid_for_legacy_rows(db_ctx):
    """历史行（batch_id 空串）回退旧 rowid 锚定口径：同秒行全部可读，向后兼容。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid, title="工程概况", content="概况" * 200)
    await _insert_ai_row(db, sid, rule_id="AIX-01", severity="high",
                         hit=False, item="历史检查项一")
    await _insert_ai_row(db, sid, rule_id="AIX-02", severity="high",
                         hit=False, item="历史检查项二")
    await db.commit()

    ov = await readiness_overview(sid, db, force=True)
    ai_rids = {f.get("rule_id") for f in ov["findings"] if f.get("mode") == "ai"}
    assert {"AIX-01", "AIX-02"} <= ai_rids, \
        "无批号的历史行必须仍按 rowid 段读出（旧库不丢结果）"


# ===========================================================================
# 二、CMP-09 / TRC-01 跨维度双扣修复（口径互补）
# ===========================================================================
def test_trc01_suppressed_when_all_calc_sections_lack_process():
    """全部计算书章节都无计算过程 → 只报 CMP-09（block），TRC-01 必须抑制。

    修复前：CMP-09（completeness -40）与 TRC-01（traceability -40）对同一
    缺陷双扣 80；两规则在注册表里同为 block 级。
    """
    ctx = _mk_ctx([
        _mk_section("支护结构计算书", "支护形式采用排桩加内支撑，详见施工图。" * 30),
        _mk_section("脚手架验算", "脚手架满足规范要求，结论合格。" * 30),
    ])
    findings = run_preflight(ctx)
    cmp09 = [f for f in findings if f["rule_id"] == "CMP-09"]
    trc01 = [f for f in findings if f["rule_id"] == "TRC-01"]
    assert cmp09, "整体无计算过程时 CMP-09（聚合口径）必须报告"
    assert not trc01, "同一缺陷不得再由 TRC-01 逐章重复报告（跨维度双扣）"


def test_trc01_fires_when_only_some_sections_lack_process():
    """整体有计算过程、个别章节缺失 → TRC-01 报告独立缺陷面，CMP-09 不发。"""
    good = "荷载计算：N = 1.25 × 200 = 250 kN，满足承载力要求。"
    bad = "支护结构详见计算简图与附图说明。" * 30
    ctx = _mk_ctx([
        _mk_section("支撑轴力计算书", good),
        _mk_section("稳定性验算", bad),
    ])
    findings = run_preflight(ctx)
    cmp09_extra = [f for f in findings
                   if f["rule_id"] == "CMP-09" and "计算过程" in f.get("detail", "")]
    trc01 = [f for f in findings if f["rule_id"] == "TRC-01"]
    assert not cmp09_extra, "聚合文本含计算过程时 CMP-09 追加条款不应触发"
    assert trc01, "个别章节缺计算过程必须仍被 TRC-01 报告（不得漏报）"
    assert trc01[0]["section_title"] == "稳定性验算"


def test_trc01_and_cmp09_both_block_severity_documented():
    """事实锁：两规则在注册表中同为 block —— 这正是双扣 80 分的量级来源。"""
    assert get_rule("TRC-01").severity == "block"
    assert get_rule("CMP-09").severity == "block"


# ===========================================================================
# 三、孤儿 API：保留端点但标记 deprecated（大版本清理信号锁）
# ===========================================================================
def _route_map(router, path: str, method: str):
    for rt in router.routes:
        if getattr(rt, "path", "") == path and method in (getattr(rt, "methods", None) or ()):
            return rt
    return None


def test_orphan_endpoints_marked_deprecated():
    from app.routers import review as _review_mod
    cases = [
        (_compliance_mod.router, "/api/v1/compliance/dimensions", "GET"),
        (_compliance_mod.router, "/api/v1/compliance/consistency-audit/{scheme_id}/history", "GET"),
        (_compliance_mod.router, "/api/v1/compliance/preflight/{scheme_id}", "POST"),
        (_review_mod.router, "/api/v1/schemes/{scheme_id}/review/statuses", "GET"),
    ]
    for router, path, method in cases:
        rt = _route_map(router, path, method)
        assert rt is not None, f"端点 {path} [{method}] 必须保留（向后兼容）"
        assert getattr(rt, "deprecated", False) is True, \
            f"孤儿端点 {path} 应在 OpenAPI 标记 deprecated"
