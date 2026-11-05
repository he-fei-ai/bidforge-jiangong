"""R95 审核与预检模块收口护栏（2026-10-17）

覆盖本轮三项修复（全部有代码依据，逐项行为实证）：

A. **批量审核 compare-and-set**（R90 遗留⑥）：`batch_review_sections` 的
   UPDATE 带期望状态，读后写窗口内被并发方改写 → 记入 `conflicts`
   （响应体加法式新键），**不落评审留痕、不假成功**。
B. **单章审核 / 方案提交 compare-and-set**：0 行如实 409；
   方案级期望值为空串时保留「非法/空状态允许直接覆盖推进」的既有语义
   （2026-09-17 修复不被回退）。
C. **CAS 谓词的 SQLite 真实语义**：桩连接只验证「参数被传到」，
   WHERE 条件本身的匹配行为用真库 sqlite3 实证（COALESCE 归一 NULL、
   `? = ''` 逃生门）。
D. **validate_rule_registry 生产消费接线**（R90 遗留①）：/rules 端点
   每次调用即双向自检，健康时 `registry_problems == []`（加法式契约），
   漂移时非空且记 ERROR 日志。
E. **静态锁**：三处 UPDATE 的 CAS 接线不被悄悄拆回；前端批量反馈
   链路透传 conflicts。
"""

from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import pytest
from app.models import SchemeReviewIn, SectionReviewIn
from app.routers import compliance
from app.routers import review as rv
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# 桩连接：可编程返回行 / rowcount，并记录全部写语句
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, row=None, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return [self._row] if self._row is not None else []


class _StubConn:
    """按 SQL 特征路由的桩。`update_rowcount` 模拟 CAS 生效(1)/被并发(0)。"""

    def __init__(self, *, section_status="pending", scheme_status="pending", update_rowcount=1, pf_row="default"):
        self.section_status = section_status
        self.scheme_status = scheme_status
        self.update_rowcount = update_rowcount
        self.pf_row = pf_row
        self.writes: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=()):
        s = " ".join(str(sql).split())
        if s.startswith("SELECT"):
            if "project_id" in s:
                return _Cur({"project_id": "p-1"})
            if "FROM sections WHERE id=? AND scheme_id=?" in s:
                return _Cur({"id": "sec-1", "title": "T", "review_status": self.section_status})
            if "review_status FROM schemes" in s:
                return _Cur({"id": "s-1", "name": "S", "status": "草稿", "review_status": self.scheme_status})
            if "FROM schemes WHERE id=?" in s:
                return _Cur({"id": "s-1"})
            if "FROM preflight_runs" in s:
                if self.pf_row == "none":
                    return _Cur(None)
                return _Cur(
                    {
                        "blocked": 0,
                        "released": 1,
                        "content_fingerprint": "",
                        "total": 88,
                        "created_at": "2026-10-17 00:00:00",
                    }
                )
            if "FROM sections WHERE scheme_id=?" in s or "MAX(updated_at)" in s:
                return _Cur(None)
            return _Cur(None)
        if s.startswith("UPDATE") or s.startswith("INSERT"):
            self.writes.append((s, tuple(params)))
            rc = self.update_rowcount if s.startswith("UPDATE") else 1
            return _Cur(None, rowcount=rc)
        return _Cur(None, rowcount=0)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _update_writes(conn: _StubConn):
    return [w for w in conn.writes if w[0].startswith("UPDATE")]


def _record_inserts(conn: _StubConn):
    return [w for w in conn.writes if "review_records" in w[0]]


# ===========================================================================
# A. 批量审核 CAS 行为实证
# ===========================================================================
class TestBatchCASFallback:
    async def test_happy_path_still_changes_and_records(self):
        conn = _StubConn(section_status="pending", update_rowcount=1)
        out = await rv.batch_review_sections(
            "s-1", {"section_ids": ["sec-1"], "to_status": "approved", "reviewer": "张三", "comment": ""}, conn
        )
        assert out["changed"] == 1 and out["conflicts"] == []
        upd = _update_writes(conn)[0]
        # CAS：WHERE 带期望状态，参数含读到的 from_status
        assert "COALESCE(review_status,'')=?" in upd[0]
        assert upd[1] == ("approved", "sec-1", "pending")
        assert len(_record_inserts(conn)) == 1

    async def test_concurrent_change_goes_to_conflicts_no_record(self):
        """并发方在 SELECT→UPDATE 窗口改写状态（0 行）→ conflicts，绝不落痕。"""
        conn = _StubConn(section_status="pending", update_rowcount=0)
        out = await rv.batch_review_sections(
            "s-1", {"section_ids": ["sec-1"], "to_status": "approved", "reviewer": "张三", "comment": ""}, conn
        )
        assert out["changed"] == 0
        assert out["conflicts"] == ["sec-1"]
        assert out["changed_ids"] == []
        assert _record_inserts(conn) == [], "CAS 失败仍写评审留痕 = 伪造轨迹"

    async def test_conflicts_key_present_even_without_race(self):
        """契约锁定：conflicts 恒存在（前端按其长度决定是否警示）。"""
        conn = _StubConn(update_rowcount=1)
        out = await rv.batch_review_sections(
            "s-1", {"section_ids": ["sec-1"], "to_status": "approved", "reviewer": "张三", "comment": ""}, conn
        )
        assert "conflicts" in out

    async def test_self_transition_never_reaches_update(self):
        """已是目标态 → nochange（幂等），CAS 语句根本不发。"""
        conn = _StubConn(section_status="approved")
        out = await rv.batch_review_sections(
            "s-1", {"section_ids": ["sec-1"], "to_status": "approved", "reviewer": "张三", "comment": ""}, conn
        )
        assert out["nochange"] == ["sec-1"]
        assert _update_writes(conn) == []


# ===========================================================================
# B. 单章审核 / 方案提交 CAS 行为实证
# ===========================================================================
class TestSectionAndSubmitCasing:
    async def test_section_race_returns_409(self):
        conn = _StubConn(section_status="pending", update_rowcount=0)
        with pytest.raises(HTTPException) as exc:
            await rv.review_section("s-1", "sec-1", SectionReviewIn(to_status="approved", reviewer="张三"), conn)
        assert exc.value.status_code == 409
        assert _record_inserts(conn) == []

    async def test_section_happy_path_returns_changed(self):
        conn = _StubConn(section_status="reviewing", update_rowcount=1)
        out = await rv.review_section("s-1", "sec-1", SectionReviewIn(to_status="approved", reviewer="张三"), conn)
        assert out["changed"] is True
        upd = _update_writes(conn)[0]
        assert "COALESCE(review_status,'')=?" in upd[0]
        assert upd[1] == ("approved", "sec-1", "reviewing")

    async def test_submit_race_returns_409_not_fake_ok(self):
        conn = _StubConn(scheme_status="pending", update_rowcount=0)
        with pytest.raises(HTTPException) as exc:
            await rv.submit_scheme_review("s-1", SchemeReviewIn(to_status="approved", reviewer="张三"), conn)
        assert exc.value.status_code == 409

    async def test_submit_illegal_legacy_status_keeps_escape_hatch(self):
        """库内残留非法 review_status：from_status 归一为空串，
        CAS 谓词 `? = ''` 放行覆盖推进（2026-09-17 既有语义不回退）。"""
        conn = _StubConn(scheme_status="已发布", update_rowcount=1)
        out = await rv.submit_scheme_review("s-1", SchemeReviewIn(to_status="approved", reviewer="张三"), conn)
        assert out["ok"] is True
        upd = _update_writes(conn)[0]
        assert "? = ''" in upd[0]
        # 空期望值出现两次（逃生门两侧参数）
        assert upd[1].count("") == 2


# ===========================================================================
# C. CAS 谓词的 SQLite 真实语义（真库实证，防「参数传了但条件写错」）
# ===========================================================================
def _fresh_db(review_status):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sections (id TEXT, review_status TEXT)")
    if review_status is not None:
        conn.execute("INSERT INTO sections VALUES ('sec-1', ?)", (review_status,))
    else:
        conn.execute("INSERT INTO sections (id) VALUES ('sec-1')")
    conn.commit()
    return conn


class TestCasSqlSemantics:
    def test_expectation_mismatch_matches_zero_rows(self):
        conn = _fresh_db("pending")
        cur = conn.execute(
            "UPDATE sections SET review_status=? WHERE id=?" " AND COALESCE(review_status,'')=?",
            ("approved", "sec-1", "reviewing"),
        )
        assert cur.rowcount == 0, "期望状态不符必须 0 行（CAS 的判据本身）"

    def test_expectation_match_and_null_coalesce(self):
        conn = _fresh_db("pending")
        cur = conn.execute(
            "UPDATE sections SET review_status=? WHERE id=?" " AND COALESCE(review_status,'')=?",
            ("approved", "sec-1", "pending"),
        )
        assert cur.rowcount == 1
        conn2 = _fresh_db(None)  # NULL 行
        cur2 = conn2.execute(
            "UPDATE sections SET review_status=? WHERE id=?" " AND COALESCE(review_status,'')=?",
            ("approved", "sec-1", ""),
        )
        assert cur2.rowcount == 1, "NULL 必须被 COALESCE 归一为空串期望所命中"

    def test_escape_hatch_semantics(self):
        conn = _fresh_db("已发布")  # 非法残留状态
        cur = conn.execute(
            "UPDATE sections SET review_status=? WHERE id=?" " AND (? = '' OR COALESCE(review_status,'') = ?)",
            ("approved", "sec-1", "", ""),
        )
        assert cur.rowcount == 1, "期望为空串时必须无条件放行（逃生门）"
        cur2 = conn.execute(
            "UPDATE sections SET review_status=? WHERE id=?" " AND (? = '' OR COALESCE(review_status,'') = ?)",
            ("approved", "sec-1", "pending", "pending"),
        )
        assert cur2.rowcount == 0, "非空期望对非法残留行必须失配"


# ===========================================================================
# D. validate_rule_registry 生产消费接线（R90 遗留①）
# ===========================================================================
class TestRulesEndpointSelfCheck:
    async def test_rules_carries_empty_registry_problems(self):
        out = await compliance.list_rules()
        assert out["registry_problems"] == [], "当前注册表健康，契约键必须存在且为空"
        # 原有契约零变化
        for k in ("rule_version", "items", "dimensions", "standard_db_version", "standard_db_checked_at"):
            assert k in out

    async def test_drift_is_surfaced_and_logged(self, monkeypatch, caplog):
        from app.services import audit_rules as ar

        saved = ar.RULE_MAP
        monkeypatch.setattr(ar, "RULE_MAP", {k: v for k, v in saved.items() if k != "DLV-13"})
        with caplog.at_level("ERROR", logger="compliance"):
            out = await compliance.list_rules()
        assert out["registry_problems"], "摘除 DLV-13 注册必须被自检发现"
        assert any("DLV-13" in p for p in out["registry_problems"])
        assert any("漂移" in r.message for r in caplog.records), "生产分叉必须落 ERROR 日志（可观测性）"

    def test_list_rules_actually_calls_validator(self):
        """静态锁：接线不许被悄悄拆回（此前生产零消费，仅 CI 触发）。"""
        src = inspect.getsource(compliance.list_rules)
        assert "validate_rule_registry(" in src
        assert "_EXPORT_ISSUE_RULE_MAP" in src and "_PROGRAM_EMITTED_RULE_IDS" in src


# ===========================================================================
# E. 静态锁：三处 CAS 接线 + 前端透传 + 陈旧锚点订正不复归
# ===========================================================================
class TestStaticWiringLocks:
    def test_three_update_sites_are_cas(self):
        for fn in (rv.batch_review_sections, rv.review_section, rv.submit_scheme_review):
            src = inspect.getsource(fn)
            assert "COALESCE(review_status,'')" in src, fn.__name__
            assert "require_rows=False" in src, fn.__name__
        # 旧裸写形态不得回流：UPDATE 字符串以 `review_status=? WHERE id=?",`
        # 直接收尾（无 CAS 谓词）。注意不能只匹配 `WHERE id=?",` ——
        # 「方案存在性校验」的 SELECT id FROM schemes WHERE id=? 同样以它收尾。
        naked = re.compile(r'review_status=\? WHERE id=\?",')
        for fn in (rv.batch_review_sections, rv.review_section):
            assert not naked.search(inspect.getsource(fn))

    def test_frontend_surfaces_conflicts(self):
        p = (
            Path(__file__).resolve().parents[2]
            / "frontend"
            / "src"
            / "components"
            / "review"
            / "ReviewWorkflowPanel.tsx"
        )
        src = p.read_text(encoding="utf-8")
        assert "data.conflicts" in src, "批量反馈链路必须透传 conflicts"
        assert "并发操作修改" in src

    def test_export_render_anchors_followed_r94_split(self):
        """基线 2 失败用例的锚点订正不复归：T7 断言必须锚渲染段。"""
        tp = Path(__file__).resolve().parent / "test_audit_fixes_20260927.py"
        src = tp.read_text(encoding="utf-8")
        seg = src[src.index("class TestAIRenderCountNotDiscardedT7") :]
        assert "inspect.getsource(_prepare_export_render)" in seg
        assert "inspect.getsource(_prepare_export)" in seg  # 取数段声明仍锚原函数
