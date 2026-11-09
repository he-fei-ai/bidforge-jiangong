# -*- coding: utf-8 -*-
"""R192 目录生成模块写入口完备性与契约旁路收口护栏（2026-11-09）。

分组结构：
  A 行为实证 —— FIX-R1 renumber src_line 行基准统一（归一化先行，错位修复）
  B 守卫棘轮 —— FIX-R2 编号修复/回滚路由、FIX-R7 实例化、FIX-R16 缺陷执行
  C auto_confirm 契约 —— FIX-R4 短方案补齐、FIX-R5 duplicate_titles 硬前置
  D 分步兜底/持久化 —— FIX-R6 L1 即时落 holder、FIX-R10 word_budget 锁
  E checkpoint 损坏可观测 —— FIX-R11 两处静默吞 → WARNING
  F 上传闸门 —— FIX-R3 validate_outline_numbering fail-closed
  G SAVEPOINT 隔离 —— FIX-R8 create_scheme 套用目录库半提交
  H 自证 —— 锚点真存在（防 vacuous pass）+ 字节体检
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
FRONTEND = BACKEND.parent / "frontend" / "src"


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


# ============================================================
# A · 行为实证（FIX-R1）
# ============================================================
class TestRenumberNormalizedLineBasis:
    """FIX-R1：_parse_content_blocks 解析前归一化会改变行数（2026-11-05 引入），
    renumber 的切行/解析/重写必须共用同一份归一化文本，否则 src_line 错位——
    重写落到标题行之前/之后的任意行（原文被整行替换成标题、真标题残留旧号）。"""

    def test_page_mark_before_heading_rewrites_true_line(self):
        """分页标记行（normalize 删行）紧邻错号标题：重写必须落在真实标题行。"""
        from app.services.numbering import renumber_section_body_subheadings

        content = "前言段落\n===== Page 2 =====\n# 9.9 错号子标题\n正文甲\n# 9.10 另一节\n正文乙"
        new, changes = renumber_section_body_subheadings(content, "2.1", 2, "资源配置")
        assert "Page 2" not in new, "归一化应删除分页标记"
        assert "9.9" not in new and "9.10" not in new, f"旧错号应被重写: {new!r}"
        assert "错号子标题" in new and "另一节" in new, "标题文本必须保留"
        assert "正文甲" in new and "正文乙" in new, "正文行不得丢失"
        heads = [ln for ln in new.split("\n") if ln.lstrip().startswith("#")]
        assert len(heads) == 2, f"标题行数恰为 2（无 Ghost 行）: {heads}"
        assert len(changes) == 2, "两处重写都应记账"

    def test_literal_newline_before_heading_rewrites_true_line(self):
        """字面 \\n 还原（增行）紧邻错号标题：重写同样不得错位。"""
        from app.services.numbering import renumber_section_body_subheadings

        content = "开工准备\\n人员进场\n# 5.5 现场布置\n浇筑完成"
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "施工部署")
        assert "开工准备" in new and "人员进场" in new, "字面换行应还原为两行"
        assert "5.5" not in new, f"旧错号应被重写: {new!r}"
        assert "现场布置" in new and "浇筑完成" in new
        assert len(changes) == 1

    def test_caption_pipe_split_before_heading_no_misalignment(self):
        """表题粘连行（normalize 拆分增行）在标题之前：重写落在真实标题行。"""
        from app.services.numbering import renumber_section_body_subheadings

        content = "表4-1 联调|系统|结果\n# 7.7 调试要点\n正文丙"
        new, changes = renumber_section_body_subheadings(content, "4.1", 2, "联调与验收")
        assert "7.7" not in new, f"旧错号应被重写: {new!r}"
        assert "调试要点" in new and "正文丙" in new, "标题与正文不得损坏"
        heads = [ln for ln in new.split("\n") if ln.lstrip().startswith("#")]
        assert len(heads) == 1, f"无 Ghost 标题行: {heads}"

    def test_idempotent_after_normalization(self):
        """归一化后再次进入零改动（normalize 幂等 → renumber 幂等）。"""
        from app.services.numbering import renumber_section_body_subheadings

        content = "前言段落\n===== Page 2 =====\n# 9.9 错号子标题\n正文甲"
        once, _ = renumber_section_body_subheadings(content, "2.1", 2, "资源配置")
        twice, changes2 = renumber_section_body_subheadings(once, "2.1", 2, "资源配置")
        assert twice == once and not changes2, "幂等性被破坏"

    def test_plain_content_passthrough_unchanged(self):
        """无标题无杂质 → 原样返回（零重写零归一化扰动）。"""
        from app.services.numbering import renumber_section_body_subheadings

        content = "普通段落一\n普通段落二"
        new, changes = renumber_section_body_subheadings(content, "1.1", 2, "某章")
        assert new == content and not changes


# ============================================================
# B · 守卫棘轮（FIX-R2 / FIX-R7 / FIX-R16）
# ============================================================
class TestGuardRatchets:
    def test_numbering_repair_route_has_generation_guard(self):
        """FIX-R2：编号一致性修复路由接 ensure_no_generation_write。"""
        src = _src("routers/sections.py")
        body = src[src.index("async def repair_numbering_consistency") :]
        body = body[: body.index("@router.get")]
        assert 'ensure_no_generation_write(scheme_id, "编号一致性修复")' in body

    def test_numbering_rollback_route_has_generation_guard(self):
        src = _src("routers/sections.py")
        body = src[src.index("async def rollback_numbering_version") :]
        body = body[: body.index("@router.post(\"/reorder\")")]
        assert 'ensure_no_generation_write(scheme_id, "编号版本回滚")' in body

    def test_repair_route_guard_blocks_during_content_generation(self, monkeypatch):
        """行为实证：正文生成中调 repair → 409（守卫先于一切写库）。"""
        import app.routers.sections as sec
        from fastapi import HTTPException

        monkeypatch.setattr(sec, "content_generation_in_progress", lambda sid: "task-abc")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(sec.repair_numbering_consistency("scheme-x", db=None))
        assert ei.value.status_code == 409
        assert "编号一致性修复" in str(ei.value.detail)

    def test_rollback_route_guard_blocks_during_outline_generation(self, monkeypatch):
        import app.routers.sections as sec
        from fastapi import HTTPException

        monkeypatch.setattr(sec, "content_generation_in_progress", lambda sid: None)
        monkeypatch.setattr(sec, "outline_generation_in_progress", lambda sid: "task-out")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(sec.rollback_numbering_version("scheme-x", "snap-1", db=None))
        assert ei.value.status_code == 409

    def test_instantiate_route_has_dual_guards_before_save(self):
        """FIX-R7：dry_run=false 实例化在 _save_outline_to_db 之前有双 409 守卫。"""
        src = _src("routers/schemes.py")
        body = src[src.index("async def instantiate_scheme_outline") :]
        i_save = body.index("from app.routers.sections import _save_outline_to_db")
        head = body[:i_save]
        assert "content_generation_in_progress(scheme_id)" in head
        assert "outline_generation_in_progress(scheme_id)" in head

    def test_defects_apply_and_decision_have_generation_guard(self):
        """FIX-R16：缺陷执行/裁决（整章覆盖写）接统一守卫。"""
        src = _src("routers/defects.py")
        assert src.count("ensure_no_generation_write") >= 2
        assert "缺陷修复执行" in src
        assert "缺陷修复裁决" in src

    def test_numbering_repair_update_recalcs_word_status(self):
        """FIX-R9：编号修复 UPDATE 补 word_status 重算（R190 FIX-12 同族补面）。"""
        src = _src("services/numbering.py")
        start = src.index("async def repair_scheme_numbering_consistency")
        end = src.index("\nasync def ", start + 1)
        body = src[start:end]
        # ⚠️ R192 A/B M12 教训（R191 M11 同族）：只锁符号在场时，变异把
        # `word_status_for(_wc, _wb)` 换成 `str(_wb)` 而 import 仍在场即假绿——
        # 判据必须锁**实际调用点**（含实参形态）。
        assert "word_status_for(_wc, _wb)" in body
        assert "UPDATE sections SET content=?, word_count=?, word_status=?" in body


# ============================================================
# C · auto_confirm 契约（FIX-R4 / FIX-R5）
# ============================================================
class TestAutoConfirmContract:
    def test_evaluate_blocks_on_duplicate_titles(self):
        """FIX-R5：issue_counts.duplicate_titles > 0 → blocker + ready=False。"""
        from app.services.outline_auto_confirm import evaluate

        res = evaluate(
            [{"title": "工程概况", "children": []}],
            {"issue_counts": {"duplicate_titles": 2}},
            [],
            has_content=False,
        )
        codes = [b["code"] for b in res["blockers"]]
        assert "duplicate_titles" in codes
        assert res["ready"] is False
        assert res["counts"]["duplicate_titles"] == 2

    def test_evaluate_without_duplicate_key_backward_compatible(self):
        """旧 review 无该键 → 计数 0、无该 blocker（逐字向后兼容）。"""
        from app.services.outline_auto_confirm import evaluate

        res = evaluate([{"title": "工程概况", "children": []}], {}, [], has_content=False)
        assert all(b["code"] != "duplicate_titles" for b in res["blockers"])
        assert res["counts"]["duplicate_titles"] == 0

    def test_review_exit_rechecks_duplicates_wired_both_exits(self):
        """FIX-R5：审核链两出口（合并修复早退 + 主出口）都接 _recheck_duplicate_titles。"""
        src = _src("routers/_sse_outline_review.py")
        assert src.count("_recheck_duplicate_titles(outline, review_obj)") == 2
        assert "def _recheck_duplicate_titles" in src
        assert src.count("_sh._detect_duplicate_titles") >= 3, "预检闸门 + 闭包共用单一实现"

    def test_short_scheme_payload_carries_auto_confirm(self):
        """FIX-R4：短方案 completed 载荷构造含 auto_confirm（与长方案同判定）。"""
        src = _src("routers/sse_handlers.py")
        i_short = src.index("_short_payload = {")
        seg = src[i_short : i_short + 2200]
        assert 'auto_confirm' in seg and "_evaluate_auto_short" in seg
        # 长方案判定仍在场（两路并存）
        assert src.count("scheme_has_content") >= 2

    def test_checkpoint_read_whitelist_includes_auto_confirm(self):
        """短方案 auto_confirm 随 checkpoint 持久化后，重挂恢复白名单可透传。"""
        from app.routers.sse_handlers import _CHECKPOINT_KINDS

        spec = _CHECKPOINT_KINDS.get("outline_generation")
        assert spec and "auto_confirm" in spec[1]


# ============================================================
# D · 分步兜底/持久化（FIX-R6 / FIX-R10）
# ============================================================
class TestStepwiseRatchets:
    def test_l1_result_lands_in_holder_immediately(self):
        """FIX-R6：L1 校验通过即落 _partial_holder（先于首个批窗归位）。"""
        src = _src("routers/sse_handlers.py")
        i_holder = src.index('_partial_holder["outline"] = level1')
        i_sub = src.index('_prog["sub_total"] = len(level1)')
        assert i_holder > i_sub, "holder 落点必须在 L1 校验之后"
        # 与首个批窗归位（full_outline）保持先后：L1 即时版在前
        i_full = src.index('_partial_holder["outline"] = full_outline')
        assert i_holder < i_full

    def test_word_budget_persist_wrapped_with_lock_and_retry(self):
        """FIX-R10：两处 word_budget 持久化持 _db_write_lock + retry_db_op。"""
        src = _src("routers/sse_handlers.py")
        assert "_persist_alloc_budget_once" in src
        assert "_persist_l3_floor_once" in src
        assert src.count("async with _db_write_lock:") >= 6, "既有点位 + 本轮 2 处新接线"
        for name in ("_persist_alloc_budget_once", "_persist_l3_floor_once"):
            i = src.index(f"async def {name}")
            seg = src[i : i + 600]
            assert "async with _db_write_lock" in seg and "retry_db_op" in seg


# ============================================================
# E · checkpoint 损坏可观测（FIX-R11）
# ============================================================
class TestCheckpointCorruptionLogging:
    @staticmethod
    def _install_fake_conn(monkeypatch, raw: str):
        import app.routers.sse_checkpoint as cs

        class _Cur:
            def __init__(self, row):
                self._row = row

            async def fetchone(self):
                return self._row

        class _Conn:
            def __init__(self, row):
                self._row = row

            async def execute(self, *a, **k):
                return _Cur(self._row)

        async def fake_get_read_conn():
            return _Conn((raw,))

        async def fake_release(conn):
            return None

        monkeypatch.setattr(cs, "get_read_conn", fake_get_read_conn)
        monkeypatch.setattr(cs, "release_read_conn", fake_release)

    def test_corrupted_checkpoint_json_logs_warning(self, monkeypatch, caplog):
        from app.routers.sse_checkpoint import _load_task_checkpoint

        self._install_fake_conn(monkeypatch, "{not-valid-json")
        with caplog.at_level(logging.WARNING, logger="sse.checkpoint"):
            res = asyncio.run(_load_task_checkpoint("task-1", "outline_result"))
        assert res is None, "既有语义不变：损坏按无成果处理"
        assert any("checkpoint_json 解析失败" in r.message for r in caplog.records)

    def test_attach_checkpoint_result_corruption_logs_warning(self, caplog):
        from app.routers.sse_handlers import _attach_checkpoint_result

        row = {"id": "task-9", "task_type": "outline_generation", "checkpoint_json": "<<<bad>>>"}
        result: dict = {}
        with caplog.at_level(logging.WARNING):
            _attach_checkpoint_result(row, result)
        assert "outline_result" not in result, "既有语义不变：不附加"
        assert any("checkpoint_json 解析失败" in r.message for r in caplog.records)

    def test_kind_mismatch_logs_info(self, caplog):
        from app.routers.sse_handlers import _attach_checkpoint_result

        row = {"id": "task-9", "task_type": "outline_generation", "checkpoint_json": '{"kind": "other"}'}
        result: dict = {}
        with caplog.at_level(logging.INFO):
            _attach_checkpoint_result(row, result)
        assert any("kind 不匹配" in r.message for r in caplog.records)


# ============================================================
# F · 上传闸门（FIX-R3）
# ============================================================
class TestUploadNumberingGate:
    def test_save_as_outline_has_validate_gate(self):
        src = _src("routers/upload_outline.py")
        i_norm = src.index("outline = normalize_outline(outline)\n    if not outline:")
        seg = src[i_norm : i_norm + 1600]
        # ⚠️ R192 A/B M8 教训（R191 M11 同族）：判据必须锁**调用点赋值链**——
        # 只锁符号在场时，变异删调用留 import 即假绿。
        assert "_numbering_problems = validate_outline_numbering(outline)" in seg
        assert "上传目录编号校验未通过" in seg

    def test_validate_outline_numbering_rejects_unnumbered_tree(self):
        """判据非空转：未编号树必须产出问题清单（400 依据）。"""
        from app.services.outline_utils import validate_outline_numbering

        problems = validate_outline_numbering([{"title": "工程概况", "children": [{"title": "临时设施"}]}])
        assert problems, "未编号树必须被检出"


# ============================================================
# G · SAVEPOINT 隔离（FIX-R8）
# ============================================================
class TestApplyLibrarySavepoint:
    @staticmethod
    def _schema_sql() -> list[str]:
        return [
            "CREATE TABLE outline_library (id TEXT PRIMARY KEY, name TEXT, version TEXT,"
            " outline_json TEXT, review_status TEXT, ref_count INTEGER DEFAULT 0)",
            "CREATE TABLE sections (id TEXT PRIMARY KEY, scheme_id TEXT, title TEXT)",
            "CREATE TABLE schemes (id TEXT PRIMARY KEY, outline_source TEXT, status TEXT)",
        ]

    def _run(self, monkeypatch, tmp_sqlite_path: str, fail: bool):
        import sqlite3

        import aiosqlite
        import app.routers.sections as sec
        from app.routers.schemes import _apply_library_on_create

        async def _main():
            db = await aiosqlite.connect(tmp_sqlite_path)
            db.row_factory = sqlite3.Row  # 生产 get_db 同款（_apply_library_on_create 按列名取值）
            try:
                for ddl in self._schema_sql():
                    await db.execute(ddl)
                await db.execute(
                    "INSERT INTO outline_library (id, name, version, outline_json, review_status)"
                    " VALUES ('lib-1', '模板A', 'v1.0', '[{\"title\": \"工程概况\", \"children\": []}]', '已通过')"
                )
                await db.execute("INSERT INTO schemes (id) VALUES ('scheme-1')")
                await db.commit()

                calls = {"n": 0}

                async def fake_save(db_, scheme_id, outline, source="", **kw):
                    calls["n"] += 1
                    await db_.execute("INSERT INTO sections (id, scheme_id, title) VALUES ('s1', ?, 'x')", (scheme_id,))
                    if fail:
                        raise RuntimeError("boom-mid-save")
                    return {"count": 1}

                monkeypatch.setattr(sec, "_save_outline_to_db", fake_save)
                result = await _apply_library_on_create(db, "scheme-1", ["lib-1"])
                cur = await db.execute("SELECT COUNT(*) FROM sections")
                (n,) = await cur.fetchone()
                return result, n, calls["n"]
            finally:
                await db.close()

        return asyncio.run(_main())

    def test_failure_rolls_back_partial_section_writes(self, monkeypatch, tmp_path):
        """套用中途异常：SAVEPOINT 回滚后 sections 零残留（方案主行不再携带半成品）。"""
        result, n_sections, calls = self._run(monkeypatch, str(tmp_path / "sp_fail.db"), fail=True)
        assert result is None, "失败降级返回 None（既有设计意图保持）"
        assert calls == 1
        assert n_sections == 0, "半套脏章节写必须被 SAVEPOINT 回滚"

    def test_success_path_keeps_writes(self, monkeypatch, tmp_path):
        result, n_sections, calls = self._run(monkeypatch, str(tmp_path / "sp_ok.db"), fail=False)
        assert result and result["library_id"] == "lib-1"
        assert calls == 1
        assert n_sections == 1

    def test_savepoint_ratchet_in_source(self):
        src = _src("routers/schemes.py")
        assert "SAVEPOINT apply_library_on_create" in src
        assert "ROLLBACK TO SAVEPOINT apply_library_on_create" in src
        assert "RELEASE SAVEPOINT apply_library_on_create" in src


# ============================================================
# H · 自证与字节体检
# ============================================================
class TestSelfEvidence:
    def test_r192_markers_present_in_all_touched_backend_files(self):
        files = [
            "services/numbering.py",
            "routers/sections.py",
            "routers/upload_outline.py",
            "routers/sse_handlers.py",
            "routers/sse_checkpoint.py",
            "services/outline_auto_confirm.py",
            "routers/_sse_outline_review.py",
            "routers/schemes.py",
            "routers/defects.py",
        ]
        for rel in files:
            assert "R192" in _src(rel), f"{rel} 缺 R192 标记"

    def test_backend_files_byte_hygiene(self):
        files = [
            "services/numbering.py",
            "routers/sections.py",
            "routers/upload_outline.py",
            "routers/sse_handlers.py",
            "routers/sse_checkpoint.py",
            "services/outline_auto_confirm.py",
            "routers/_sse_outline_review.py",
            "routers/schemes.py",
            "routers/defects.py",
        ]
        for rel in files:
            raw = (APP / rel).read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 引入 BOM"
            assert "\ufffd" not in raw.decode("utf-8", errors="strict"), f"{rel} 含替换符（编码损坏）"
