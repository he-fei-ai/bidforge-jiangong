# -*- coding: utf-8 -*-
"""R51 · 遗留收口回归护栏（2026-10-07）

覆盖 R50 报告「未落地」清单的三项：

A. ``consistency_conflicts`` 陈旧判定（指纹单一事实源）
   一致性扫描冲突此前**不参与**陈旧排除 —— 正文已变后，旧扫描的未解决冲突
   仍按最近一批计入就绪度评分。修法：扫描落库时写当前正文指纹（与总检聚合
   同一算法，services/scheme_fingerprint 单一事实源），聚合时比对跳过。

B. 深层点分伪标题（≥4 段）识别与重排
   AI 在正文写 4~7 段点分伪标题（如 7.3.3.1.1 材料计划）时旧实现只识别到
   3 段 —— 更深的行被当普通段落，既不参与编号规范化（父章节重排后前缀错位
   残留成稿）也不按标题样式渲染。现按「段数 + 1」识别（封顶 7）。

C. STD-03 装饰装修材料标准库（openstd.samr.gov.cn 实证，2026-10-07）
   GB 18582-2020 已废止 → GB 30981.1-2025；GB 18583-2008 现行；
   GB 18580-2017 已废止 → GB 18580-2025；GB/T 39600-2021 现行。
"""
from __future__ import annotations

import json
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.routers import compliance as _cc
from app.routers.compliance import _readiness_overview_compute
from app.services import scheme_fingerprint
from app.services.standards_registry import (
    ABOLISHED_STANDARDS,
    CATEGORY_STANDARDS,
    STANDARD_DB_VERSION,
)


# ===========================================================================
# A. 指纹单一事实源 + consistency_conflicts 陈旧判定
# ===========================================================================
@pytest.fixture
async def ov_db(tmp_path):
    _appdb.DB_PATH = tmp_path / "r51-conflicts.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget)"
        " VALUES(?,?,?,?,0)", (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "", "一、工程概况", "", 1,
         "generated", 200, 0, "本工程概况正文" * 50, "", 0))
    await db.commit()
    yield db, sid
    await close_db()


async def _insert_scan_conflicts(db, sid, *, fingerprint="", scan_id=None):
    scan_id = scan_id or uuid.uuid4().hex
    await db.execute(
        "INSERT INTO consistency_conflicts (id, scheme_id, scan_id,"
        " conflict_type, severity, topic, occurrences,"
        " authoritative_value, repair_instruction, reason, status,"
        " content_fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, scan_id, "工期", "high", "开工日期不一致",
         json.dumps(["旧正文一处", "旧正文二处"], ensure_ascii=False),
         "2026-01-01", "按开工令为准", "两处引用开工日期不同", "pending",
         fingerprint, "2026-10-07T12:00:00"))
    await db.commit()


class TestFingerprintSingleSource:
    async def test_compliance_delegates_to_shared(self, ov_db):
        """compliance._content_fingerprint 与 scheme_fingerprint 必须同一算法。"""
        db, sid = ov_db
        a = await _cc._content_fingerprint(db, sid)
        b = await scheme_fingerprint.content_fingerprint(db, sid)
        assert a == b != ""

    async def test_fingerprint_changes_when_content_changes(self, ov_db):
        db, sid = ov_db
        before = await scheme_fingerprint.content_fingerprint(db, sid)
        await db.execute(
            "UPDATE sections SET content=? WHERE scheme_id=?",
            ("改了的内容" * 30, sid))
        await db.commit()
        after = await scheme_fingerprint.content_fingerprint(db, sid)
        assert before != after


class TestOverviewSkipsStaleScanConflicts:
    async def test_stale_scan_excluded_and_reported(self, ov_db):
        db, sid = ov_db
        await _insert_scan_conflicts(db, sid, fingerprint="stale-old")
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "consistency_scan" not in payload["sources"], payload["sources"]
        assert "consistency_scan" in payload["stale_ai_sources"]
        assert not [f for f in payload["findings"]
                    if f["rule_id"].startswith("CON-SCAN")]

    async def test_fresh_scan_included(self, ov_db):
        db, sid = ov_db
        fp = await scheme_fingerprint.content_fingerprint(db, sid)
        await _insert_scan_conflicts(db, sid, fingerprint=fp)
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "consistency_scan" in payload["sources"]
        assert payload["stale_ai_sources"] == []
        assert [f for f in payload["findings"]
                if f["rule_id"].startswith("CON-SCAN")]

    async def test_historical_scan_without_fingerprint_fail_open(self, ov_db):
        """历史行（无指纹）不得判过期（与 _run_is_stale 同约定）。"""
        db, sid = ov_db
        await _insert_scan_conflicts(db, sid, fingerprint="")
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "consistency_scan" in payload["sources"]
        assert payload["stale_ai_sources"] == []
        assert [f for f in payload["findings"]
                if f["rule_id"].startswith("CON-SCAN")]


# ===========================================================================
# B. 深层点分伪标题（≥4 段）
# ===========================================================================
class TestDeepPlainHeading:
    def test_deep_number_detected(self):
        from app.services.content_blocks import _detect_plain_heading
        # 4 段 → 层级 5；5 段 → 层级 6
        assert _detect_plain_heading("7.3.3.1.1 材料计划") == (6, "材料计划")
        assert _detect_plain_heading("3.2.1.1 关键节点") == (5, "关键节点")

    def test_three_level_unchanged(self):
        from app.services.content_blocks import _detect_plain_heading
        assert _detect_plain_heading("3.2.1 工序") == (4, "工序")

    def test_year_number_rejected(self):
        from app.services.content_blocks import _detect_plain_heading
        assert _detect_plain_heading("2023.5.1.1 年份") is None

    def test_too_deep_rejected(self):
        from app.services.content_blocks import _detect_plain_heading
        assert _detect_plain_heading("1.2.3.4.5.6.7.8 超深") is None

    def test_sentence_tail_rejected(self):
        from app.services.content_blocks import _detect_plain_heading
        assert _detect_plain_heading("7.3.3.1.1 这是一句正文。") is None

    def test_parse_marks_deep_heading(self):
        from app.services.content_blocks import _parse_content_blocks
        blocks = _parse_content_blocks("7.3.3.1.1 材料计划\n正文内容。")
        assert blocks and blocks[0]["type"] == "heading"

    def test_renumber_fixes_wrong_prefix(self):
        """父章节重排后，深层伪标题的前缀必须随父章节修正（错误 7.3.3 前缀清除）。

        注意：编号规范化工作在**展示域**（存储 "3.2" 的展示前缀是 "2"，
        AGENTS §4.23/§4.25 同口径）—— 断言用 stored_id_to_prefix 动态计算，
        不写死 "3.2"。
        """
        from app.services.numbering import (
            renumber_section_body_subheadings,
            stored_id_to_prefix,
        )
        content = "7.3.3.1.1 材料计划\n\n正文内容。"
        out, changes = renumber_section_body_subheadings(
            content, "3.2", 2, "3.2 施工计划", has_db_children=False)
        assert changes, "深层伪标题必须被规范化改写"
        assert "7.3.3" not in out, "错误前缀必须被清除"
        prefix = stored_id_to_prefix("3.2")
        assert f"###### {prefix}.1 材料计划" == out.split("\n")[0], \
            "必须改用父章节展示前缀（相对深度 0 → 第 1 个子标题）"

    def test_deep_renumber_idempotent(self):
        from app.services.numbering import renumber_section_body_subheadings
        content = "7.3.3.1.1 材料计划\n\n正文内容。"
        out, _ = renumber_section_body_subheadings(
            content, "3.2", 2, "3.2 施工计划", has_db_children=False)
        out2, ch2 = renumber_section_body_subheadings(
            out, "3.2", 2, "3.2 施工计划", has_db_children=False)
        assert ch2 == [], "规范化必须幂等"
        assert out2 == out


# ===========================================================================
# C. STD-03 装饰装修材料标准库（openstd 实证）
# ===========================================================================
class TestStandardsRegistryR51:
    def test_decorating_material_standards_added(self):
        codes = {s.code for s in CATEGORY_STANDARDS["装饰保温"]}
        assert {"GB 18583-2008", "GB 30981.1-2025",
                "GB 18580-2025", "GB/T 39600-2021"} <= codes

    def test_abolished_added(self):
        assert "GB 18582-2020" in ABOLISHED_STANDARDS
        assert "GB 18580-2017" in ABOLISHED_STANDARDS

    def test_version_bumped(self):
        assert STANDARD_DB_VERSION == "2026.10.7"
