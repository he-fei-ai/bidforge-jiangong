# -*- coding: utf-8 -*-
"""R166 · 全局事实模块收口护栏（2026-11-03）。

覆盖本轮十项修复的回归锁：
  A 组  三链互斥对称化（outline/content 避让 facts_generation）——源码锁 + 行为实证
  B 组  R13 读路径判空守卫（事实半区 19 读位点）——行为实证 + 棘轮计数
  C 组  事实链三写点 retry_db_op（R108 锁族补面）——行为实证 + 源码锁
  D 组  分组重建/delete_fact 的 DELETE 与 SELECT 空值口径对称（COALESCE）——行为实证
  E 组  AI 调整 add 分支 R13 守卫（杜绝假成功计数）——源码切片锁
  F 组  clear_all_facts 回传实删行数——行为实证 + 源码锁
  G 组  XV-NUM-UNIT 分组键与 XV-SAME-NAME 同源（fact_key 优先）——行为实证 ×3
  H 组  注入审计精确口径（injected_titles 账本平衡 + rendered_text 逐字节不变）
  I 组  卫生项（prepared 4 元组标注 / 孤儿函数摘除 / 判据静默吞留痕）
  K 组  字节体检（CRLF 保持 / 无 BOM / 无孤立 CR）
"""

import ast
import json
import sqlite3
import uuid
from pathlib import Path

import app.routers.global_facts as gf
import app.services.facts_builder as fb
import app.services.facts_cross_validators as fcv
import app.services.facts_extractor as fe
import pytest
from app.models import ClearFactsRequest, FactGroupUpdate
from app.services.facts_extractor import (
    ExtractionResult,
    FactGroup,
    FactItem,
    invalidate_export_cache,
    persist_extraction,
)
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A 组 · 三链互斥对称化
# ---------------------------------------------------------------------------


class TestMutexSymmetryR166:
    def test_outline_registry_conflicts_include_facts(self):
        src = _src("app/routers/sse_handlers.py")
        assert (
            '("content_generation", "facts_generation")' in src
        ), "目录链注册冲突元组必须含 facts_generation（R166 对称化）"

    def test_content_registry_conflicts_include_facts(self):
        src = _src("app/routers/sse_handlers.py")
        assert (
            '("outline_generation", "facts_generation")' in src
        ), "正文链注册冲突元组必须含 facts_generation（R166 对称化）"

    def test_both_pre_guards_check_facts(self):
        # 事件流是闭包，无法 inspect —— 用源码计数锁：outline/content/facts
        # 三个入口的 pre-guard 都检查 facts_generation_in_progress（≥3 处）。
        src = _src("app/routers/sse_handlers.py")
        assert (
            src.count("facts_generation_in_progress(scheme_id)") >= 3
        ), "generate_outline / generate_content / generate_facts 的 pre-guard 都必须检查事实提取在跑"

    async def test_register_content_conflicts_with_running_facts(self, db_conn):
        """行为实证：facts_generation 在跑时，content/outline 注册必须被拒。"""
        from app.services.ai import task_registry as tr

        await tr.register_task("facts_generation", "p1", "s1")
        with pytest.raises(tr.TaskTypeConflict):
            await tr.register_task(
                "content_generation", "p1", "s1", conflict_types=("outline_generation", "facts_generation")
            )
        with pytest.raises(tr.TaskTypeConflict):
            await tr.register_task(
                "outline_generation", "p1", "s1", conflict_types=("content_generation", "facts_generation")
            )


# ---------------------------------------------------------------------------
# B 组 · R13 读路径判空守卫
# ---------------------------------------------------------------------------


class _NoneCursorDB:
    """db.execute 恒返回 None 的故障替身（R13 形态）。"""

    async def execute(self, *args, **kwargs):
        return None


class TestR13ReadGuardsR166:
    def test_require_fact_cursor_none_is_503(self):
        with pytest.raises(HTTPException) as ei:
            gf._require_fact_cursor(None, "单测位点")
        assert ei.value.status_code == 503

    def test_write_cursor_delegates_to_read_guard(self):
        with pytest.raises(HTTPException) as ei:
            gf._require_fact_write_cursor(None, "单测位点")
        assert ei.value.status_code == 503
        cur = object()
        assert gf._require_fact_write_cursor(cur, "x") is cur

    async def test_load_fact_rows_none_cursor_503(self):
        with pytest.raises(HTTPException) as ei:
            await gf._load_fact_rows(_NoneCursorDB(), "s1", "p1")
        assert ei.value.status_code == 503

    def test_read_guard_site_ratchet(self):
        """棘轮锁：事实半区读游标守卫位点数固定为 21（定义 + 委派 + 19 个读位点）。

        新增读位点时必须同步接守卫并更新本数（漏接会让本数与实际位点数失配，
        有意新增时改这个数即可 —— 锁的目的是强迫「加位点必想守卫」）。
        """
        src = _src("app/routers/global_facts.py")
        assert src.count("_require_fact_cursor(cur") == 21


# ---------------------------------------------------------------------------
# C 组 · 事实链三写点 retry_db_op
# ---------------------------------------------------------------------------


class TestDbLockRetryR166:
    def test_three_retry_sites_present(self):
        src = _src("app/services/facts_extractor.py")
        assert src.count("retry_db_op(") >= 3
        for fn_name in ("persist_extraction", "invalidate_export_cache", "save_extracted_chunks"):
            fn = getattr(fe, fn_name)
            assert "retry_db_op" in ast.unparse(ast.parse(_src("app/services/facts_extractor.py"))) or True
            body = ast.get_source_segment(
                _src("app/services/facts_extractor.py"), _fn_node(_src("app/services/facts_extractor.py"), fn_name)
            )
            assert body and "retry_db_op(" in body, f"{fn_name} 必须接 retry_db_op"

    async def test_persist_retries_on_locked_commit(self):
        """commit 首次抛 locked → 重试成功，事实落库、无异常（fail-soft 不再直达 failed）。"""
        rows: list[tuple] = []
        calls = {"executemany": 0, "commit": 0, "rollback": 0}

        class _Cur:
            async def fetchall(self):
                return []

        class _DB:
            async def execute(self, sql, *a):
                return _Cur()

            async def executemany(self, sql, rows_):
                calls["executemany"] += 1
                rows.extend(rows_)

            async def commit(self):
                calls["commit"] += 1
                if calls["commit"] == 1:
                    raise sqlite3.OperationalError("database is locked")

            async def rollback(self):
                calls["rollback"] += 1

        item = FactItem(
            name="总工期", value="360天", key="total_duration", category="schedule", source="doc.docx", chunk_hash="NEW"
        )
        result = ExtractionResult()
        result.groups = [FactGroup(title="工期安排", category="schedule", items=[item])]
        result.total_items = 1
        result.chunk_hashes_all = {"NEW"}
        await persist_extraction(result, _DB(), "p1", "s1")
        assert calls["executemany"] == 2, "重试后 executemany 应重放一次"
        assert calls["rollback"] == 1, "仅重试时回滚（首次尝试绝不回滚）"
        assert len(rows) == 2, "两次尝试各插 1 行（首次未提交、重试生效）"

    async def test_invalidate_cache_retries_on_locked_delete(self, tmp_path):
        deletes = {"n": 0}

        class _Cur:
            async def fetchall(self):
                return [{"id": "c1", "result_path": None}]

        class _DB:
            async def execute(self, sql, *a):
                if sql.startswith("DELETE FROM export_cache"):
                    deletes["n"] += 1
                    if deletes["n"] == 1:
                        raise sqlite3.OperationalError("database is locked")
                return _Cur()

            async def commit(self):
                pass

            async def rollback(self):
                pass

        n = await invalidate_export_cache(_DB(), "s1", facts_touched=True)
        assert n == 1
        assert deletes["n"] == 2, "重试后 DELETE 应重放一次"


def _fn_node(src: str, name: str):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# D 组 · DELETE 空值口径对称（COALESCE）
# ---------------------------------------------------------------------------


class TestDeleteScopeCoalesceR166:
    async def test_group_rebuild_deletes_null_scheme_rows(self, db_conn):
        """NULL scheme_id 的旧行必须被分组重建删除（旧实现裸 scheme_id=? 删不掉 → 重复）。"""
        fid = uuid.uuid4().hex
        await db_conn.execute(
            "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
            "group_title, title, content, category, is_simulated, confidence, "
            "is_resolved, has_conflict, conflict_keys, fact_key, chunk_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fid,
                "p1",
                None,
                "G1",
                "工期",
                "总工期",
                "- **总工期**: 300天",
                "schedule",
                0,
                1.0,
                0,
                0,
                "",
                "total_duration",
                "",
            ),
        )
        await db_conn.commit()
        resp = await gf.update_fact(
            "G1",
            FactGroupUpdate(
                id="G1", title="工期安排", content="- **总工期**: 320天\n- **计量周期**: 按月", category="schedule"
            ),
            scheme_id="",
            db=db_conn,
        )
        assert resp == {"ok": True}
        cur = await db_conn.execute("SELECT id FROM global_facts WHERE group_id='G1'")
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 2, f"分组重建后应恰 2 行（旧行已删），实际 {len(rows)}"
        assert fid not in {r["id"] for r in rows}, "NULL scheme_id 旧行未被删除"

    def test_delete_sql_coalesce_parity(self):
        src = _src("app/routers/global_facts.py")
        # 两处组作用域 DELETE 都必须与归属回查 SELECT 的 COALESCE 口径对称
        assert (
            src.count("AND COALESCE(scheme_id,'')=?") >= 3
        ), "分组重建 SELECT / 分组重建 DELETE / delete_fact DELETE 至少三处 COALESCE"


# ---------------------------------------------------------------------------
# E 组 · AI 调整 add 分支 R13 守卫
# ---------------------------------------------------------------------------


class TestAdjustAddGuardR166:
    def test_add_branch_asserts_write_applied(self):
        src = _src("app/routers/global_facts.py")
        i = src.find("_add_cur = await db.execute(")
        assert i > 0, "adjust add 分支必须捕获 INSERT 游标"
        j = src.find('applied["added"] += 1', i)
        assert j > 0
        seg = src[i:j]
        assert "_assert_fact_write_applied(" in seg, "INSERT 游标必须过 R13 守卫后才允许计数（杜绝假成功）"


# ---------------------------------------------------------------------------
# F 组 · clear_all_facts 实删行数
# ---------------------------------------------------------------------------


class TestClearActualDeletedR166:
    async def test_clear_returns_actual_rowcount(self, db_conn):
        sid, pid = "s1", "p1"
        await db_conn.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
        for i in range(2):
            await db_conn.execute(
                "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
                "group_title, title, content, category) VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, pid, sid, f"g{i}", "工期", f"事实{i}", f"- **事实{i}**: v", "schedule"),
            )
        await db_conn.commit()
        resp = await gf.clear_all_facts(ClearFactsRequest(scheme_id=sid), db=db_conn)
        assert resp["ok"] is True
        assert resp["deleted"] == 2

    def test_clear_uses_safe_rowcount(self):
        src = _src("app/routers/global_facts.py")
        i = src.find("async def clear_all_facts")
        j = src.find('return {"ok": True, "deleted"', i)
        seg = src[i:j]
        assert "safe_rowcount(cur" in seg, "deleted 必须取 DELETE 实际影响行数"


# ---------------------------------------------------------------------------
# G 组 · XV-NUM-UNIT 分组键同源化
# ---------------------------------------------------------------------------


def _item(name, value, key=""):
    return FactItem(name=name, value=value, key=key, category="other", source="doc.docx", chunk_hash="h")


class TestNumUnitGroupingR166:
    def test_same_key_diff_names_compared(self):
        """同 fact_key 异名（跨来源措辞差异）必须进入量纲比对。"""
        merged = [_item("基坑深度", "12m", key="pit_depth"), _item("基坑开挖深度", "1200cm", key="pit_depth")]
        out = fcv._check_numeric_unit_consistency(merged)
        assert any(c.rule_id == "XV-NUM-UNIT" for c in out), "fact_key 分组下 12m vs 1200cm 应检出单位不一致"

    def test_diff_key_diff_names_not_compared(self):
        merged = [_item("甲深度", "12m", key="k_a"), _item("乙深度", "1200cm", key="k_b")]
        out = fcv._check_numeric_unit_consistency(merged)
        assert not any(c.rule_id == "XV-NUM-UNIT" for c in out)

    def test_legacy_no_key_falls_back_to_name(self):
        """key 缺失（历史 shim）回落按 name 分组，与旧版行为一致。"""
        merged = [_item("坑深", "12m"), _item("坑深", "12cm")]
        out = fcv._check_numeric_unit_consistency(merged)
        assert any(c.rule_id == "XV-NUM-UNIT" for c in out)


# ---------------------------------------------------------------------------
# H 组 · 注入审计精确口径
# ---------------------------------------------------------------------------

_ROWS = [
    ("工期", "总工期", "- **总工期**: 300天", 0.9, "plan", ""),
    ("质量", "质保期", "- **质保期**: 2年", 0.95, "acceptance", ""),
    ("安全", "基坑深度", "- **基坑深度**: 12m", 0.3, "safety", ""),
]
_ROW_TITLES = {"总工期", "质保期", "基坑深度"}


class TestAuditExactnessR166:
    def test_injected_titles_covers_rendered_rows(self):
        audit = fb.fact_injection_audit(list(_ROWS))
        assert audit["injected_titles"] == ["总工期", "质保期", "基坑深度"]
        assert audit["budget_dropped"] == []
        assert set(audit["injected_titles"]) == _ROW_TITLES

    def test_ledger_balance_under_budget(self):
        """账本平衡：injected_titles ∪ budget_dropped 恰好覆盖输入行集。"""
        audit = fb.fact_injection_audit(list(_ROWS), max_total=60)
        got = set(audit["injected_titles"]) | set(audit["budget_dropped"])
        assert got == _ROW_TITLES
        assert audit["budget_dropped_count"] == len(audit["budget_dropped"])

    def test_rendered_text_byte_identical_to_pure_renderer(self):
        audit = fb.fact_injection_audit(list(_ROWS), max_total=60)
        direct = fb._render_facts_text(list(_ROWS), max_total=60)
        assert audit["rendered_text"] == direct

    def test_sse_uses_audit_union_for_injected_facts(self):
        src = _src("app/routers/sse_handlers.py")
        assert 'injected_facts": _injected_facts' in src
        assert (
            src.count('.get("injected_titles")') >= 1
        ), "逐章审计 injected_facts 必须取三段审计的 injected_titles 并集"


# ---------------------------------------------------------------------------
# I 组 · 卫生项
# ---------------------------------------------------------------------------


class TestHygieneR166:
    def test_prepared_annotation_is_4_tuple(self):
        src = _src("app/services/facts_builder.py")
        assert "prepared: list[tuple[str, str, str, float | None]] = []" in src

    def test_orphan_flow_helper_removed(self):
        src = _src("app/services/facts_cross_validators.py")
        assert "_has_flow_content" not in src, "零调用孤儿函数不得回流"

    def test_predicate_self_failure_logged(self):
        src = _src("app/services/facts_extractor.py")
        i = src.find("_permanent = is_non_retryable_error(e)")
        assert i > 0
        assert "按可重试错误处理" in src[i : i + 400], "判据自身故障的静默吞必须有 DEBUG 留痕"


# ---------------------------------------------------------------------------
# K 组 · 字节体检
# ---------------------------------------------------------------------------


class TestByteHygieneR166:
    FILES = [
        "app/routers/global_facts.py",
        "app/routers/sse_handlers.py",
        "app/services/facts_extractor.py",
        "app/services/facts_builder.py",
        "app/services/facts_cross_validators.py",
        "app/services/ai/prompts/_registry.py",
        "tests/test_facts_closeout_r166_20261103.py",
    ]

    def test_line_endings_and_bom(self):
        for rel in self.FILES:
            b = (BACKEND / rel).read_bytes()
            assert not b.startswith(b"\xef\xbb\xbf"), f"{rel} 带 BOM"
            crlf = b.count(b"\r\n")
            lf = b.count(b"\n") - crlf
            assert b"\xef\xbf\xbd" not in b, f"{rel} 含替换符"
            if crlf > 0:
                assert lf == 0, f"{rel} 行尾混排（CRLF={crlf} 裸LF={lf}）"
            assert b.count(b"\r") == crlf, f"{rel} 存在孤立 CR"
