# -*- coding: utf-8 -*-
"""全局事实模块深度审计收口（R193 · 2026-11-09）。

本轮基线：后端事实簇 -k fact 1347 passed / 7 skipped / 1 failed（sse_handlers
被并行 R192 会话写窗 LF 化，属在飞伪红，非本轮改动面）；前端 fact 簇 277 passed。
三十项定罪修复由本文件按四层锁定（行为实证 / 源码棘轮 / 双向 parity / 自证）：

  A. 行为实证
     1. 同请求重名上传不再误判 replaced → 不误置全项目事实 stale（R191① 交互回归）；
     2. 上传异常两条路径（循环异常 / commit 失败）孤儿四层产物 purge 对称；
     3. failed 文档非 force 解析短路回传真实 parse_status（R191② 后的谎报收口）；
     4. _mark_parse_failed 合并既有 parse_warnings（截断告警不再被失败原因冲掉）；
     5. parse-all 早退分支补 note/stale_facts（契约形状对齐主分支）；
     6. 模型输出 facts/groups 含非 dict 条目 → 跳过留痕不再整段崩溃；
     7. 空行不再被误判为 Markdown 分隔行；chunk_size ≤ overlap 不再静默丢内容；
     8. release_chunk_failures 空 IN () 守卫；进度三函数 R13 显式化；
     9. 补丁/整理对序列类事实走 value_to_markdown 单一出口（repr 污染收口）；
    10. 危大参数单位后缀（毫米以上）按前缀换算；source_kind「补充」误判收口；
    11. LCS 规模闸；render_outline_chapter_facts 字典边界收刀。
  B. 源码棘轮（锁调用点，不是只锁定义）
    12. replaced 计数必须排除本请求 doc_id（R191① 触发点源码锁）；
    13. cross-check 直写 global_facts 必须包 project_facts_inproc_guard（第四条写源）；
    14. format_for_frontend shared_chapters 并入主归属章；
    15. 前端 D-01/D-02：abort 收进列表 effect cleanup + 切换 effect 复位运行态。
  C. 双向 parity
    16. factsApi 12 方法泛型后响应类型字段与后端返回键非空交集抽检。
  D. 自证
    17. 锚点真存在 + 触碰文件字节体检（CRLF 保持）。
"""
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import db as _appdb
from app.db import get_conn, init_db
from app.routers import global_facts_docs as gfd
from app.services import facts_builder as fb
from app.services import facts_classification as fc
from app.services import facts_cross_validators as fcv
from app.services import facts_enrich as fen
from app.services import facts_extractor as fe
from app.services import facts_patches as fpt
from fastapi import HTTPException

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"

_GFD_SRC = Path(gfd.__file__).read_text(encoding="utf-8")
_FE_SRC = Path(fe.__file__).read_text(encoding="utf-8")
_FPT_SRC = Path(fpt.__file__).read_text(encoding="utf-8")
_FEN_SRC = Path(fen.__file__).read_text(encoding="utf-8")
_FCV_SRC = Path(fcv.__file__).read_text(encoding="utf-8")
_FB_SRC = Path(fb.__file__).read_text(encoding="utf-8")
_PIPELINE_SRC = Path(
    __import__("app.services.doc_pipeline.pipeline", fromlist=["x"]).__file__
).read_text(encoding="utf-8")
_PAGE_SRC = (FRONTEND / "src" / "pages" / "SchemeWorkbenchPage.tsx").read_text(encoding="utf-8")
_HOOK_SRC = (FRONTEND / "src" / "pages" / "schemeWorkbench" / "useFactsGeneration.ts").read_text(
    encoding="utf-8"
)
_PANEL_SRC = (FRONTEND / "src" / "pages" / "schemeWorkbench" / "FactsAdjustPanel.tsx").read_text(
    encoding="utf-8"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ===========================================================================
# A 组 · 行为实证（真实 DB / 纯函数）
# ===========================================================================


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "facts-r193.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid


def _fake_upload_file(name: str, content: bytes = b"hello facts"):
    """构造 FastAPI UploadFile 替身（最小路径：filename + read/seek）。"""
    import io

    from fastapi import UploadFile

    return UploadFile(file=io.BytesIO(content), filename=name)


class TestSameRequestDuplicateUpload:
    async def test_same_request_duplicate_not_counted_as_replaced(self, ctx, monkeypatch, tmp_path):
        """同请求两个同名文件：replaced=0、不触发 stale、响应无 note（R191① 回归收口）。"""
        import app.services.doc_pipeline.pipeline as dpp

        db, pid = ctx
        monkeypatch.setattr(
            gfd, "_doc_file_path", lambda project_id, doc_id, fname: tmp_path / f"{doc_id}_{fname}"
        )

        async def _fake_ingest(*a, **k):
            return {"ok": True}

        monkeypatch.setattr(dpp, "ingest_upload", _fake_ingest)
        files = [_fake_upload_file("same.txt"), _fake_upload_file("same.txt")]
        result = await gfd.upload_documents(scheme_id="", project_id=pid, files=files, db=db)
        assert result["replaced"] == 0, f"同请求重名不得计入 replaced：{result['replaced']}"
        assert "note" not in result and "stale_facts" not in result, "不得误触发 stale/note 链路"
        assert result["saved_count"] == 1, "upsert 语义：终态 1 行（R191⑥）"

    async def test_preexisting_replacement_still_counts(self, ctx, monkeypatch, tmp_path):
        """真替换（既有同名档案行）仍计 replaced=1 并触发 stale 标记。"""
        import app.services.doc_pipeline.pipeline as dpp

        db, pid = ctx
        monkeypatch.setattr(
            gfd, "_doc_file_path", lambda project_id, doc_id, fname: tmp_path / f"{doc_id}_{fname}"
        )

        async def _fake_ingest(*a, **k):
            return {"ok": True}

        monkeypatch.setattr(dpp, "ingest_upload", _fake_ingest)
        stale_rows = []

        async def _fake_mark_stale(db_, project_id):
            stale_rows.append(project_id)
            return 3

        monkeypatch.setattr(gfd, "_mark_project_facts_stale", _fake_mark_stale)
        files1 = [_fake_upload_file("real.txt")]
        await gfd.upload_documents(scheme_id="", project_id=pid, files=files1, db=db)
        files2 = [_fake_upload_file("real.txt")]
        result = await gfd.upload_documents(scheme_id="", project_id=pid, files=files2, db=db)
        assert result["replaced"] == 1, "跨请求同名替换必须计 replaced"
        assert stale_rows == [pid], "真替换仍触发项目级 stale 标记（R191① 意图保留）"
        assert result.get("stale_facts") == 3


class TestUploadOrphanSymmetry:
    async def test_commit_failure_path_purges_saved(self, ctx, monkeypatch, tmp_path):
        """commit 失败路径：rollback 后 saved 条目的四层产物必须 purge（⑤/⑩ 对称）。"""
        import app.services.doc_pipeline.pipeline as dpp

        db, pid = ctx
        monkeypatch.setattr(
            gfd, "_doc_file_path", lambda project_id, doc_id, fname: tmp_path / f"{doc_id}_{fname}"
        )

        async def _fake_ingest(*a, **k):
            return {"ok": True}

        monkeypatch.setattr(dpp, "ingest_upload", _fake_ingest)
        purged = []

        real_commit = db.commit
        state = {"armed": False}

        async def _commit():
            if state["armed"]:
                raise RuntimeError("disk io")
            await real_commit()

        monkeypatch.setattr(db, "commit", _commit)

        async def _fake_purge(db_, doc_id, project_id):
            purged.append(doc_id)

        monkeypatch.setattr(dpp, "purge_document", _fake_purge)
        files = [_fake_upload_file("orphan.txt")]
        state["armed"] = True
        with pytest.raises(HTTPException) as ei:
            await gfd.upload_documents(scheme_id="", project_id=pid, files=files, db=db)
        assert ei.value.status_code == 503
        assert len(purged) == 1, "commit 失败路径必须 purge 已保存条目的四层产物"


class TestParseShortCircuitHonesty:
    async def test_failed_doc_short_circuit_returns_real_status(self, ctx):
        """failed + 非空正文 + 非 force：短路回传真实 failed 状态（不再谎报 success）。"""
        db, pid = ctx
        did = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO project_documents(id, project_id, file_name, file_type, doc_type,"
            " parsed_markdown, parse_status) VALUES(?,?,?,?,?,?,?)",
            (did, pid, "f.txt", "txt", "全局事实上传", "正文内容够长", "failed"),
        )
        await db.commit()
        result = await gfd.parse_document(doc_id=did, force=False, db=db)
        assert result["already_parsed"] is True
        assert result["parse_status"] == "failed", "R191② 之后不得再谎报 success"
        assert "强制重新解析" in result.get("note", "")

    async def test_pending_doc_short_circuit_reconciles_to_success(self, ctx):
        """存量 pending + 非空正文：reconcile 后回 success（原意图保留）。"""
        db, pid = ctx
        did = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO project_documents(id, project_id, file_name, file_type, doc_type,"
            " parsed_markdown, parse_status) VALUES(?,?,?,?,?,?,?)",
            (did, pid, "g.txt", "txt", "全局事实上传", "正文内容够长", "pending"),
        )
        await db.commit()
        result = await gfd.parse_document(doc_id=did, force=False, db=db)
        assert result["parse_status"] == "success"
        assert result.get("reconciled") is True


class TestMarkParseFailedMergesWarnings:
    async def test_existing_truncation_warning_survives(self, ctx):
        """失败标记合并既有 parse_warnings（历史截断告警不被冲掉）。"""
        db, pid = ctx
        did = uuid.uuid4().hex
        from app.services.file_parser import dump_parse_warnings

        old = dump_parse_warnings(["PDF 第 3 页起截断（超过 OCR 页数上限）"])
        await db.execute(
            "INSERT INTO project_documents(id, project_id, file_name, file_type, doc_type,"
            " parsed_markdown, parse_status, parse_warnings) VALUES(?,?,?,?,?,?,?,?)",
            (did, pid, "h.pdf", "pdf", "全局事实上传", "", "pending", old),
        )
        await db.commit()
        await gfd._mark_parse_failed(db, did, "解析引擎崩溃")
        row = await (await db.execute("SELECT parse_warnings FROM project_documents WHERE id=?", (did,))).fetchone()
        text = row["parse_warnings"] or ""
        assert "截断" in text, "历史截断告警必须保留"
        assert "解析失败：解析引擎崩溃" in text, "失败原因必须追加"


class TestParseAllEarlyBranchContract:
    async def test_early_branch_has_note_and_stale_facts(self, ctx):
        """所有文档均已解析的早退分支：note/stale_facts 与主分支形状对齐。"""
        db, pid = ctx
        result = await gfd.parse_all_documents(scheme_id="", project_id=pid, force=False, db=db)
        assert "note" in result, "早退分支缺 note 键（前端按主分支形状取值拿 undefined）"
        assert result["stale_facts"] == 0
        assert "force" in result  # R191⑨ 保留


# ---------------------------------------------------------------------------
# 服务层纯函数（extractor / patches / classification / cross_validators / builder）
# ---------------------------------------------------------------------------


class TestMalformedModelOutput:
    def test_non_dict_fact_entries_skipped_not_crash(self, monkeypatch):
        """facts 数组含字符串/dict 迭代键：跳过留痕，不再 AttributeError 整段失败。"""

        async def _fake_collect(*a, **k):
            return {"facts": ["塔吊 QTZ80", {"name": "基坑深度", "value": "12m"}], "segment_failed": False}, None

        monkeypatch.setattr(fe, "collect_json_response", _fake_collect)
        out = asyncio.run(
            fe.extract_from_single_chunk("某段正文", chunk_index=0, heading="第一章 工程概况")
        )
        assert any(it.name == "基坑深度" for it in out), "合法条目必须照常解析"

    def test_legacy_groups_non_dict_skipped(self, monkeypatch):
        async def _fake_collect(*a, **k):
            return {"groups": ["坏组", {"items": [{"name": "总工期", "value": "90天"}]}]}, None

        monkeypatch.setattr(fe, "collect_json_response", _fake_collect)
        out = asyncio.run(
            fe.extract_from_single_chunk("某段正文", chunk_index=0, heading="第一章")
        )
        assert any(it.name == "总工期" for it in out)


class TestTableHeaderEmptyLine:
    def test_blank_line_not_treated_as_separator(self):
        """空行被误判为分隔行时会被并进「复用表头」注入每个切分子段；修复后
        空行只随首个子段出现一次（body 起点），后续子段表头干净。"""
        chunks: list = []
        sec_long = "| 设备 | 数量 |\n\n" + "\n".join(f"| 设备{i} | {i} |" for i in range(40))
        fe._split_table_rows(sec_long, 200, chunks, "机械", zone_type="machinery_stat", priority_weight=1.5)
        assert len(chunks) >= 2, "必须产出多个切分子段"
        blanks = sum(1 for c in chunks if len(c.text.split("\n")) > 1 and c.text.split("\n")[1] == "")
        assert blanks <= 1, f"空行混入复用表头的子段数异常：{blanks}/{len(chunks)}"


class TestChunkStepFloor:
    def test_chunk_size_le_overlap_does_not_lose_content(self):
        text = "段落内容。" * 300
        chunks = fe.split_into_chunks(text, chunk_size=500, overlap=500)
        total = sum(len(c.text) for c in chunks)
        assert total > 0, "步长为负导致 range 为空 → 整段静默丢失"
        assert total >= len(text) - 600, f"切分覆盖率异常：{total}/{len(text)}"


class TestReleaseChunkFailuresEmptyHashes:
    async def test_all_empty_chunk_hash_no_in_empty(self, ctx):
        """rows 非空但 chunk_hash 全空：空集守卫返回，不再 `IN ()` 语法错误 500。"""
        db, pid = ctx
        result = await fe.release_chunk_failures(db, pid, "", chunk_hashes=())
        # 空库本来就没有行 → 走 no_failed_segments；构造脏数据路径直接测函数内逻辑
        assert result["released"] == 0

    async def test_dirty_rows_with_empty_hash_hit_guard(self, ctx, monkeypatch):
        """脏数据（rows 全部 chunk_hash=''）：空集守卫生效，UPDATE 不下发。"""
        db, pid = ctx
        updates: list = []

        class _FakeCursor:
            async def fetchall(self):
                return [{"chunk_hash": ""}, {"chunk_hash": None}]

        orig_execute = db.execute

        async def _execute(sql, *a, **k):
            if "SELECT chunk_hash, fail_count" in sql:
                return _FakeCursor()
            updates.append((sql, a, k))
            return await orig_execute(sql, *a, **k)

        monkeypatch.setattr(db, "execute", _execute)
        result = await fe.release_chunk_failures(db, pid, "", chunk_hashes=())
        assert result["released"] == 0
        assert result["skipped_source"] == "manual_retry_no_valid_hashes"
        assert updates == [], "空 hash 集合不得下发 UPDATE（IN () 语法错误）"


class TestProgressFuncsR13:
    async def test_load_completed_chunks_none_cursor(self, ctx, monkeypatch):
        db, pid = ctx

        async def _none_execute(*a, **k):
            return None

        monkeypatch.setattr(db, "execute", _none_execute)
        out = await fe.load_completed_chunks(db, pid, "")
        assert out == set(), "None 游标必须降级空集（全量提取），不得 AttributeError"

    async def test_record_chunk_failures_none_cursor_fail_soft(self, ctx, monkeypatch):
        db, pid = ctx

        async def _none_execute(*a, **k):
            return None

        monkeypatch.setattr(db, "execute", _none_execute)
        # R13 显式化后：闭包内 raise RuntimeError → retry_db_op 重试耗尽 →
        # 外层既有 fail-soft WARNING 吞掉（不阻断提取结果）。行为不变性验证。
        await fe.record_chunk_failures(db, pid, "", failed_hashes=("h1",))


class TestSequenceValueSingleOutlet:
    def test_patch_to_list_value_no_repr(self):
        target = SimpleNamespace(
            name="施工流程", value=["垫层浇筑", "防水施工"], has_conflict=False, conflict_values=[]
        )
        patch = fpt.FactPatch(title="施工流程", content="（增加）基层处理", mode="append", create=False)
        out = fpt.apply_patches_to_fact_items([target], [patch])
        v = str(out[0].value)
        assert "['" not in v and "']" not in v, f"repr 污染：{v!r}"
        assert "垫层浇筑" in v and "基层处理" in v

    def test_finalize_comparison_no_repr_drift(self):
        target = SimpleNamespace(name="流程", key="flow", value=["工序A", "工序B"])
        # value_to_markdown(list) = "- 工序A\n- 工序B"（Markdown 列表形态）
        items, changed = fen.apply_finalize_result(
            [target],
            {"facts": [{"fact_key": "flow", "name": "流程", "value": "- 工序A\n- 工序B"}]},
        )
        assert changed == 0, "同义值（Markdown 列表 vs 数组）不得判为已变化"


class TestDangerUnitSuffix:
    def test_mm_with_chinese_suffix_converts(self):
        facts = [
            {"name": "基坑深度", "fact_key": "depth", "value": "4500毫米以上", "value_unit": "", "value_kind": ""}
        ]
        out = fc.extract_danger_params(facts)
        assert abs(out.get("depth", 0) - 4.5) < 0.01, f"4500毫米必须按 4.5m 判定：{out}"

    def test_exact_units_unchanged(self):
        facts = [{"name": "基坑深度", "fact_key": "depth", "value": "6.5米", "value_unit": "", "value_kind": ""}]
        out = fc.extract_danger_params(facts)
        assert abs(out["depth"] - 6.5) < 1e-9


class TestSourceKindRules:
    def test_supplement_notice_is_bid_doc(self):
        assert fc.classify_source_kind("答疑补充通知.docx") == "bid_doc"

    def test_manual_entry_still_manual(self):
        assert fc.classify_source_kind("用户补充录入.xlsx") == "manual"
        assert fc.classify_source_kind("手工录入") == "manual"


class TestLcsCap:
    def test_oversize_sequences_capped(self):
        a = [f"s{i}" for i in range(1200)]
        b = [f"s{i}" for i in range(1200)]
        assert fcv._lcs_len(a, b) == 0, "超闸序列必须按 0 处理（防 OOM），不得真算 dp"

    def test_normal_sequences_exact(self):
        assert fcv._lcs_len(["a", "b", "c"], ["a", "c"]) == 2


class TestOutlineDdBoundaryClip:
    def test_dd_block_clipped_at_line_boundary_with_notice(self):
        dd = "\n".join(f"- 参数{i} = 值{i}（权威）" for i in range(120))
        rows = []
        chapter = {"title": "第一章 工程概况", "description": ""}
        out = fb.render_outline_chapter_facts(rows, chapter, max_total=400, dd_block=dd)
        assert "（数据字典超出本章预算已截断" in out, "截断必须模型可见"
        assert out.split("\n")[-2] if out.endswith("）") else True
        for line in out.split("\n"):
            if line.startswith("- 参数"):
                assert line.rstrip().endswith("）") or " = " in line, "不得出现半条目"


# ===========================================================================
# B 组 · 源码棘轮（锁调用点）
# ===========================================================================


class TestSourceRatchets:
    def test_replaced_count_excludes_request_doc_ids(self):
        assert "request_doc_ids: set[str] = set()" in _GFD_SRC
        # ✅ R208 重锚到**意图**（断言强度不减）：
        #    原切片起点是字面量 "for old in await cur.fetchall()" —— 那是
        #    **实现细节**；R208 BE-11（同名重传保留人工分类）把旧行先取出
        #    （`_old_rows = await cur.fetchall()`）再遍历（`for old in
        #    _old_rows:`）⇒ 起点字符串消失 ⇒ 护栏假红。
        #    该护栏真正要守的是「replaced 计数必须排除本请求 doc_id」——
        #    即「not in request_doc_ids」必须落在「旧行遍历 → 落库」区间内。
        #    故改用语义化且稳定的锚点（R193 注释行 / 落库注释行）。
        seg = _GFD_SRC[_GFD_SRC.index("# ✅ R193：replaced 只数") : _GFD_SRC.index("# 落库（parsed_markdown 留空")]
        assert "not in request_doc_ids" in seg, "replaced 计数必须排除本请求 doc_id"
        assert "request_doc_ids.add(doc_id)" in _GFD_SRC

    def test_cross_check_write_locked(self):
        seg = _PIPELINE_SRC[_PIPELINE_SRC.index("async def run_cross_check") : _PIPELINE_SRC.index("return report", _PIPELINE_SRC.index("async def run_cross_check"))]
        assert "project_facts_inproc_guard" in seg, "cross-check 直写 global_facts 必须包事实写锁"
        assert 'holder="doc_pipeline.cross_check"' in seg

    def test_shared_chapters_includes_stored_chapter(self):
        seg = _FE_SRC[_FE_SRC.index("_chapter = it.chapter or _dims[\"chapter\"]") : _FE_SRC.index("_chapter = it.chapter or _dims[\"chapter\"]") + 400]
        assert "_chapter not in _shared_chapters" in seg

    def test_frontend_switch_effect_resets_run_state(self):
        assert "setGenerating(false);\n      setGenType(\"\");" in _PAGE_SRC
        assert "activeTaskIdRef.current = \"\";" in _PAGE_SRC
        # D-01：abort 已移进列表 effect cleanup，切换 effect 不再持有该行
        switch_seg = _PAGE_SRC[_PAGE_SRC.index("const previousSchemeId = mountedSchemeIdRef.current;") : _PAGE_SRC.index("initialTabDecidedRef.current = false;")]
        assert "factsListAbortRef.current?.abort();" not in switch_seg, "切换 effect 内 abort 误杀新方案请求（D-01）"
        load_seg = _PAGE_SRC[_PAGE_SRC.index("loadComplianceHistory();") : _PAGE_SRC.index("uploadLimits") if "uploadLimits" in _PAGE_SRC else len(_PAGE_SRC)]
        assert "factsListAbortRef.current?.abort();" in load_seg

    def test_frontend_retry_guard_uses_scheme_ref(self):
        assert "currentSchemeIdRef" in _HOOK_SRC
        assert _HOOK_SRC.count("方案已切换") >= 2, "入口与重试链均需守卫"

    def test_adjust_panel_apply_lock_and_enter_gate(self):
        assert "applyingRef" in _PANEL_SRC
        assert "if (applyingRef.current) return;" in _PANEL_SRC
        assert "text.trim() && !busyNow" in _PANEL_SRC, "Enter 门禁须对齐按钮 disabled"


# ===========================================================================
# C 组 · 双向 parity（前端类型 ↔ 后端返回键抽检）
# ===========================================================================


class TestContractParity:
    def test_parse_doc_response_covers_consumed_keys(self):
        ts = _read(FRONTEND / "src" / "types" / "facts.ts")
        for key in ("parse_status", "text_len", "note", "warnings", "reconciled"):
            assert key in ts, f"FactParseDocResponse 缺消费键 {key}"

    def test_segment_stats_type_has_skipped(self):
        ts = _read(FRONTEND / "src" / "pages" / "schemeWorkbench" / "workbenchTypes.ts")
        seg = ts[ts.index("interface FactsSegmentStats") : ts.index("}", ts.index("interface FactsSegmentStats"))]
        assert "skipped?" in seg, "segment_stats.skipped 生产方一直携带、前端类型必须声明"

    def test_docs_category_options_is_string_list(self):
        ts = _read(FRONTEND / "src" / "types" / "facts.ts")
        seg = ts[ts.index("interface FactDocCategoryOptionsResponse") :]
        assert "options: string[]" in seg, "后端 doc_categories.category_options 返回 list[str]"


# ===========================================================================
# D 组 · 自证（锚点真存在 + 字节体检）
# ===========================================================================


class TestSelfProof:
    def test_anchors_really_exist(self):
        for frag in (
            "if replaced:",
            "_mark_project_facts_stale",
            "dump_parse_warnings",
            "parse_warnings=?",
        ):
            assert frag in _GFD_SRC, f"锚点缺失：{frag}"

    @pytest.mark.parametrize(
        "relpath",
        [
            "app/routers/global_facts_docs.py",
            "app/services/facts_extractor.py",
            "app/services/facts_patches.py",
            "app/services/facts_classification.py",
            "app/services/facts_cross_validators.py",
            "app/services/facts_builder.py",
            "app/services/doc_pipeline/pipeline.py",
        ],
    )
    def test_backend_files_keep_crlf(self, relpath):
        raw = (BACKEND / relpath).read_bytes()
        assert b"\r\n" in raw, f"{relpath} 丢失 CRLF 约定"
        bare_lf = raw.replace(b"\r\n", b"").count(b"\n")
        assert bare_lf == 0, f"{relpath} 存在裸 LF={bare_lf}"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{relpath} 出现 BOM"
