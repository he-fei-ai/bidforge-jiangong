"""R176（2026-11-04）可见性收口回归。

覆盖四项：
1. 目录写库「单一闸门」：同一份含空壳一级章的目录树，走 `save-as-outline`
   与走「上传识别保存」两条入口结论一致（默认都拦 / 显式 allow 都放行且
   回传同一空壳清单）；并加**源码级护栏**：任何同时做
   `INSERT INTO sections` + `DELETE FROM sections` 的函数必须是白名单里的
   两条入口，且函数体内必须出现闸门标记。
2. D-09：落库前自动改写失败（fail-soft）必须回传 `generic_autofix_failed`，
   两条入口同口径。
3. D-23：甘特图 >30 任务被截断的计数可从载荷复算（与渲染侧同一上限/口径），
   且导出侧确实把它并入 `X-Chart-Render-Stats`。
4. D-41：超长目录「章节分片 + 分批送审」，`partial=False` /
   `truncated_sections=[]`，跨批结果按规则号合并。
5. D-41 收口：并发跑批复用全局闸门（不自造信号量）、预算闸门返回
   `partial=true` + 未审清单、批次审计可观测、建议跨批去重并集。
6. B-18：AI severity 锚定规则注册表（不允许 AI 产出 block）。
7. B-19：长文本阈值单一事实源（/check、一致性审计、扫描共用同一常量）。
"""

import ast
import json
import pathlib
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.models import ComplianceCheckIn
from app.routers import compliance as compliance_mod
from app.routers.sections import _save_outline_to_db
from app.routers.upload_outline import save_as_outline
from fastapi import HTTPException

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, sid, pid
    await close_db()


def _node(node_id: str, title: str, children: list | None = None) -> dict:
    return {"id": node_id, "title": title, "children": children or []}


def _same_tree() -> list:
    """同一份「一级章有空壳」的目录：甲有子章、乙无子章。"""
    return [
        _node(uuid.uuid4().hex, "工程概况", [_node(uuid.uuid4().hex, "工程简介")]),
        _node(uuid.uuid4().hex, "施工部署"),
    ]


async def _seed_upload(db, upload_id: str):
    await db.execute("INSERT INTO uploaded_outlines (id, status) VALUES (?, 'parsed')", (upload_id,))


async def _seed_sections(db, sid: str, pid: str, items: list) -> None:
    """items = [(title, content)]，按 order 落库。"""
    for so, (title, content) in enumerate(items):
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " level, sort_order, status, content, word_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, pid, "", title, 1, so, "done", content, len(content)),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# 1. 两条入口的闸门结论一致性 + 单一写库路径护栏
# ---------------------------------------------------------------------------
class TestOutlineGateParity:
    async def test_same_tree_both_rejected_by_default(self, ctx):
        db, sid, _ = ctx
        tree = _same_tree()
        with pytest.raises(HTTPException) as save_err:
            await _save_outline_to_db(db, sid, tree, source="手动编辑")
        up = uuid.uuid4().hex
        await _seed_upload(db, up)
        with pytest.raises(HTTPException) as up_err:
            await save_as_outline(up, {"scheme_id": sid, "outline": tree}, db=db)
        assert save_err.value.status_code == up_err.value.status_code == 400
        # 两条入口的判据/计数/章节定位/逃生舱提示必须一致（措辞可按入口微调）
        for detail in (str(save_err.value.detail), str(up_err.value.detail)):
            assert "1 个无子章节的一级章" in detail
            assert "第2章「施工部署」" in detail
            assert "allow_empty_chapters=true" in detail
        cur = await db.execute("SELECT COUNT(*) c FROM sections WHERE scheme_id=?", (sid,))
        assert (await cur.fetchone())["c"] == 0

    async def test_same_tree_both_allowed_with_explicit_flag(self, ctx):
        db, sid, _ = ctx
        tree = _same_tree()
        saved = await _save_outline_to_db(db, sid, tree, source="手动编辑", allow_empty_chapters=True)
        up = uuid.uuid4().hex
        await _seed_upload(db, up)
        uploaded = await save_as_outline(up, {"scheme_id": sid, "outline": tree, "allow_empty_chapters": True}, db=db)
        assert saved["ok"] is True and uploaded["ok"] is True
        # 同一份树 → 同一空壳清单（index/title 完全一致）
        assert saved["empty_top_chapters"] == uploaded["empty_top_chapters"]

    async def test_ai_partial_save_is_documented_divergence(self, ctx):
        """AI 部分成果是**已知例外**：自动放行但必须回传清单（不是漏检）。"""
        db, sid, _ = ctx
        tree = _same_tree()
        ai = await _save_outline_to_db(db, sid, tree, source="ai", lock_roots=True)
        assert ai["ok"] is True
        assert ai["empty_top_chapters"] == [{"index": 1, "title": "施工部署"}]


class TestSingleOutlineWritePathGuard:
    def test_only_two_gated_rebuild_paths_exist(self):
        """源码级护栏：整树重建写库只允许三条入口，且都必须引用闸门。

        ✅ R179（2026-11-05）：新增第三条 —— `services/outline_versions.py` 快照回退。
        它同样是「DELETE 全表 + 逐行 INSERT」的整树重建，故必须纳入闸门判据；
        与另两条的差异是：回退语义为还原用户此前确认过的状态，空壳清单**只回传
        不阻断**（阻断会把撤销功能锁死），但闸门实现必须是同一个（禁止各写一份）。
        """
        allowed = {
            ("routers/sections.py", "_save_outline_to_db"),
            ("routers/upload_outline.py", "save_as_outline"),
            ("services/outline_versions.py", "restore_outline_snapshot"),
        }
        found = set()
        for path in APP_DIR.rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:  # pragma: no cover - 语法错误由别的测试兜底
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                seg = ast.get_source_segment(src, node) or ""
                if "INSERT INTO sections" in seg and "DELETE FROM sections" in seg:
                    rel = path.relative_to(APP_DIR).as_posix()
                    found.add((rel, node.name))
                    assert "_detect_empty_top_chapters" in seg, f"{rel}:{node.name} 整树写库未过空壳一级章闸门"
        assert found == allowed


# ---------------------------------------------------------------------------
# 2. D-09：自动改写失败可见
# ---------------------------------------------------------------------------
class TestAutofixFailedVisible:
    async def test_manual_save_reports_autofix_failure(self, ctx, monkeypatch):
        db, sid, _ = ctx
        import app.services.outline_quality as oq
        import app.services.outline_title_fix as otf

        monkeypatch.setattr(oq, "detect_generic_titles", lambda _outline: [{"path": "1", "title": "专项6"}])

        async def _boom(*_a, **_kw):
            raise RuntimeError("ai timeout")

        monkeypatch.setattr(otf, "autofix_generic_titles_on_save", _boom)
        tree = [_node(uuid.uuid4().hex, "甲", [_node(uuid.uuid4().hex, "甲-1")])]
        result = await _save_outline_to_db(db, sid, tree, source="手动编辑")
        assert result["ok"] is True
        assert result.get("generic_autofix_failed") is True
        assert "generic_autofixed" not in result

    async def test_upload_save_reports_autofix_failure(self, ctx, monkeypatch):
        db, sid, _ = ctx
        import app.services.outline_quality as oq
        import app.services.outline_title_fix as otf

        monkeypatch.setattr(
            oq, "detect_generic_titles_for_write", lambda _outline, **_kw: [{"path": "1", "title": "专项6"}]
        )

        async def _boom(*_a, **_kw):
            raise RuntimeError("ai timeout")

        monkeypatch.setattr(otf, "autofix_generic_titles_on_save", _boom)
        up = uuid.uuid4().hex
        await _seed_upload(db, up)
        tree = [_node(uuid.uuid4().hex, "编制依据"), _node(uuid.uuid4().hex, "工程概况")]
        result = await save_as_outline(up, {"scheme_id": sid, "outline": tree}, db=db)
        assert result["ok"] is True
        assert result.get("generic_autofix_failed") is True


# ---------------------------------------------------------------------------
# 3. D-23：甘特图截断计数（复算口径 + 导出接线护栏）
# ---------------------------------------------------------------------------
class TestGanttTruncatedVisible:
    def test_json_payload_count(self):
        from app.services.ai.mermaid_gantt import (
            _GANTT_MAX_TASKS,
            gantt_truncated_task_count,
        )

        tasks = [{"name": f"t{i}", "start": "2026-01-01", "duration": "1d"} for i in range(_GANTT_MAX_TASKS + 5)]
        code = json.dumps({"tasks": tasks}, ensure_ascii=False)
        assert gantt_truncated_task_count(code) == 5
        # 恰在上限 = 未截断；空/脏载荷不误报
        assert gantt_truncated_task_count(json.dumps({"tasks": tasks[:_GANTT_MAX_TASKS]})) == 0
        assert gantt_truncated_task_count("") == 0
        assert gantt_truncated_task_count("{bad") == 0

    def test_mermaid_payload_count(self):
        from app.services.ai.mermaid_gantt import (
            _GANTT_MAX_TASKS,
            gantt_truncated_task_count,
        )

        lines = ["gantt", "    dateFormat YYYY-MM-DD", "    section S"]
        for i in range(_GANTT_MAX_TASKS + 3):
            lines.append(f"    任务{i} :2026-01-01, 2d")
        code = "\n".join(lines)
        assert gantt_truncated_task_count(code) == 3

    def test_export_wires_stats(self):
        """接线护栏：导出侧必须把计数写入 X-Chart-Render-Stats（render_stats
        整体 JSON 序列化进响应头，键入 dict 即随头发出）。"""
        src = (APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
        assert "gantt_truncated_task_count" in src
        assert 'render_stats["gantt_truncated"]' in src


# ---------------------------------------------------------------------------
# 4. D-41：章节分片 + 分批送审
# ---------------------------------------------------------------------------
class TestComplianceBatching:
    def test_merge_results_policy(self):
        from app.routers.compliance import _merge_check_results

        merged: dict = {}
        _merge_check_results(
            merged,
            [
                {"rule_id": "R1", "item": "项1", "hit": False, "severity": "低", "suggestion": ""},
                {"rule_id": "R2", "item": "项2", "hit": True, "severity": "中", "suggestion": "补"},
            ],
        )
        _merge_check_results(
            merged,
            [
                {"rule_id": "R1", "item": "项1", "hit": True, "severity": "高", "suggestion": "整改"},
                {"rule_id": "R2", "item": "项2", "hit": True, "severity": "中", "suggestion": "另一条建议"},
            ],
        )
        assert len(merged) == 2
        assert merged["R1"]["hit"] is True
        assert merged["R1"]["severity"] == "高"
        assert merged["R1"]["suggestion"] == "整改"
        # ✅ D-41：建议是「去重并集」，而不是只留首条（旧实现会丢后续批次建议）；
        #    suggestion 兼容旧消费方取并集首条。
        assert merged["R2"]["suggestions"] == ["补", "另一条建议"]
        assert merged["R2"]["suggestion"] == "补"

    def test_same_issue_three_suggestions_union(self):
        """验收：同一问题在 3 批给出 3 条不同建议 → 合并后有 3 条，非 1 条。"""
        from app.routers.compliance import _merge_check_results

        merged: dict = {}
        for sev, sug in (("低", "补附件"), ("中", "补计算书"), ("高", "补专家论证")):
            _merge_check_results(
                merged, [{"rule_id": "CMP-04", "item": "施工工艺", "hit": False, "severity": sev, "suggestion": sug}]
            )
        row = merged["CMP-04"]
        assert row["suggestions"] == ["补附件", "补计算书", "补专家论证"]
        assert len(row["suggestions"]) == 3
        assert row["severity"] == "高"  # 保留最高 severity

    async def test_long_outline_split_into_batches(self, ctx, monkeypatch):
        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        # A 章 > 单章上限（触发续段），B 章使总数 > 单批上限（触发第二批）
        from app.routers.compliance import SECTION_CONTENT_CAP

        a, b = uuid.uuid4().hex, uuid.uuid4().hex
        a_body = "甲章开头" + ("甲" * (SECTION_CONTENT_CAP + 500))
        b_body = "乙章开头" + ("乙" * 3500)
        for nid, title, content, so in ((a, "甲章", a_body, 0), (b, "乙章", b_body, 1)):
            await db.execute(
                "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
                " level, sort_order, status, content, word_count)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (nid, sid, pid, "", title, 1, so, "done", content, len(content)),
            )
        await db.commit()

        prompts: list[str] = []

        async def _fake_collect(messages, _validator, **_kw):
            prompts.append(messages[0]["content"])
            sev = "中" if len(prompts) == 1 else "高"
            return (
                {"results": [{"rule_id": rid, "item": "规则项", "hit": True, "severity": sev, "suggestion": "整改"}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        out = await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)

        assert out["batch_count"] == len(prompts) == 2
        assert out["partial"] is False
        assert out["truncated_sections"] == []
        # 分片覆盖：两章都进过模型，A 章的续段标记真实存在
        assert any("甲章开头" in p for p in prompts)
        assert any("乙章开头" in p for p in prompts)
        assert any("（续 2/2）" in p for p in prompts)
        # 跨批合并：同一规则只留一行，严重度取更高一档
        assert len(out["results"]) == 1
        assert out["results"][0]["severity"] == "高"

    async def test_small_outline_keeps_single_batch(self, ctx, monkeypatch):
        """小目录行为不变：1 批、partial=false、无未审清单。"""
        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        await _seed_sections(db, sid, pid, [("第一章", "短正文" * 100)])
        calls: list[int] = []

        async def _fake_collect(_m, _v, **_kw):
            calls.append(1)
            return (
                {"results": [{"rule_id": rid, "item": "项", "hit": True, "severity": "low", "suggestion": ""}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        out = await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)
        assert out["batch_count"] == out["executed_batch_count"] == len(calls) == 1
        assert out["partial"] is False
        assert out["truncated_sections"] == []

    async def test_budget_gate_returns_partial_and_unchecked(self, ctx, monkeypatch):
        """超预算闸门：审到上限即停，返回 partial + 未审章节清单（已审不白跑）。"""
        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        # 两章各 ~5000 字 → 两批（每批一章）
        await _seed_sections(db, sid, pid, [("甲章", "甲" * 5000), ("乙章", "乙" * 5000)])
        monkeypatch.setattr(compliance_mod.settings, "compliance_max_batches", 1)
        prompts: list[str] = []

        async def _fake_collect(messages, _v, **_kw):
            prompts.append(messages[0]["content"])
            return (
                {"results": [{"rule_id": rid, "item": "项", "hit": True, "severity": "low", "suggestion": ""}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        out = await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)

        assert out["batch_count"] == 2
        assert out["executed_batch_count"] == 1
        assert len(prompts) == 1  # 只跑了 1 批，已审部分保留
        assert out["partial"] is True
        assert out["budget"] == {"max_batches": 1, "max_chars": compliance_mod.settings.compliance_max_chars}
        # 未审清单：只列出被跳过的乙章，且状态/字数可核
        assert [x["section_title"] for x in out["truncated_sections"]] == ["乙章"]
        _un = out["truncated_sections"][0]
        assert _un["status"] == "not_checked"
        assert _un["unchecked_chars"] > 0
        # 已审部分不白跑：结果仍然返回
        assert len(out["results"]) == 1

    async def test_batch_audit_rows_observable(self, ctx, monkeypatch):
        """ai_audit_logs 能看出「第几批/共几批/本批字符数」，且不新增表。"""
        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        await _seed_sections(db, sid, pid, [("甲章", "甲" * 5000), ("乙章", "乙" * 5000)])

        async def _fake_collect(_m, _v, **_kw):
            return (
                {"results": [{"rule_id": rid, "item": "项", "hit": True, "severity": "low", "suggestion": ""}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        out = await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)
        assert out["batch_count"] == 2
        rows = await (
            await db.execute(
                "SELECT action, scene, error, success FROM ai_audit_logs"
                " WHERE action='compliance_batch' ORDER BY error"
            )
        ).fetchall()
        assert len(rows) == 2
        assert all(r["scene"] == "compliance_check" and r["success"] == 1 for r in rows)
        assert "batch=1/2 chars=" in rows[0]["error"]
        assert "batch=2/2 chars=" in rows[1]["error"]

    async def test_budget_gate_writes_gate_row(self, ctx, monkeypatch):
        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        await _seed_sections(db, sid, pid, [("甲章", "甲" * 5000), ("乙章", "乙" * 5000)])
        monkeypatch.setattr(compliance_mod.settings, "compliance_max_batches", 1)

        async def _fake_collect(_m, _v, **_kw):
            return (
                {"results": [{"rule_id": rid, "item": "项", "hit": True, "severity": "low", "suggestion": ""}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)
        gate = await (
            await db.execute(
                "SELECT error FROM ai_audit_logs WHERE action='compliance_batch'" " AND error LIKE '%budget_hit=1%'"
            )
        ).fetchall()
        assert len(gate) == 1
        assert "unchecked_sections=1" in gate[0]["error"]


class TestComplianceConcurrencyGate:
    def test_no_self_made_semaphore(self):
        """并发不自造闸门：compliance.py 不得出现 Semaphore，且确实用 gather；
        真实全局上限由 provider_factory.chat_with_fallback 的控制器兜底。"""
        src = (APP_DIR / "routers" / "compliance.py").read_text(encoding="utf-8")
        assert "Semaphore(" not in src
        assert "asyncio.gather" in src
        pf = (APP_DIR / "services" / "ai" / "provider_factory.py").read_text(encoding="utf-8")
        assert "concurrency_controller.semaphore" in pf

    async def test_batches_run_concurrently(self, ctx, monkeypatch):
        """gather 路径确实并发（在飞峰值 > 1），批序不变。"""
        import asyncio as _aio

        db, sid, pid = ctx
        from app.services.audit_rules import ai_rules

        rid = ai_rules()[0].rule_id
        await _seed_sections(db, sid, pid, [("甲章", "甲" * 5000), ("乙章", "乙" * 5000)])
        state = {"inflight": 0, "peak": 0}

        async def _fake_collect(_m, _v, **_kw):
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            await _aio.sleep(0.01)
            state["inflight"] -= 1
            return (
                {"results": [{"rule_id": rid, "item": "项", "hit": True, "severity": "low", "suggestion": ""}]},
                None,
            )

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        out = await compliance_mod.compliance_check(ComplianceCheckIn(scheme_id=sid, rule_ids=[rid]), db)
        assert out["batch_count"] == 2
        assert state["peak"] == 2


class TestAiSeverityAnchored:
    """B-18：AI severity 必须锚定规则注册表，且永不产出 block。"""

    def test_anchor_policy(self):
        from app.services.audit_scoring import _anchor_ai_severity

        # SAF-01 注册表 high：AI low → 最多下调一档到 medium
        assert _anchor_ai_severity("SAF-01", "low") == "medium"
        # AI 永远不能产出 block（即使它说 high 以外更严重）
        assert _anchor_ai_severity("SAF-01", "block") == "high"
        # 注册表外的自造规则号也不允许 block
        assert _anchor_ai_severity("CUSTOM-X", "block") == "high"
        # 注册表 block 的规则不允许被 AI 下调
        from app.services.audit_rules import ALL_RULES

        _blocks = [r.rule_id for r in ALL_RULES if r.severity == "block"]
        if _blocks:
            assert _anchor_ai_severity(_blocks[0], "low") == "block"

    def test_findings_use_anchored_severity(self):
        from app.services.audit_scoring import ai_results_to_findings

        out = ai_results_to_findings(
            [
                {"rule_id": "SAF-01", "item": "风险源", "hit": False, "severity": "low", "suggestion": "补"},
            ]
        )
        assert len(out) == 1
        assert out[0]["severity"] == "medium"
        # "疑似 + low" 仍按 AI 原始档位跳过，不因锚定变成扣分项
        assert ai_results_to_findings([{"rule_id": "SAF-01", "item": "风险源", "hit": None, "severity": "low"}]) == []


class TestLongTextThresholdUnified:
    """B-19：长文本阈值单一事实源（内容检查 / 一致性审计 / 扫描同口径）。"""

    def test_single_source(self):
        from app.services import consistency_scanner as cs
        from app.services.numeric_topics import (
            LONG_TEXT_CALL_CAP,
            LONG_TEXT_SECTION_CAP,
        )

        assert compliance_mod.SECTION_CONTENT_CAP == LONG_TEXT_SECTION_CAP
        assert compliance_mod.AI_CONTENT_HARD_CAP == LONG_TEXT_CALL_CAP
        assert compliance_mod.SECTION_CONTENT_CAP == cs.PER_SECTION_LIMIT
        assert compliance_mod.AI_CONTENT_HARD_CAP == cs.LONG_TEXT_SECTION_CAP

    def test_scanner_clip_is_marked(self):
        from app.services import consistency_scanner as cs

        text = "甲" * (cs.LONG_TEXT_SECTION_CAP + 1000)
        clipped = cs._clip_section_content(text)
        assert clipped.startswith("甲" * 20)
        assert f"本章正文已截断，原文 {len(text)} 字" in clipped

    async def test_consistency_audit_batches_and_merges(self, ctx, monkeypatch):
        db, sid, pid = ctx
        await _seed_sections(db, sid, pid, [("甲章", "甲" * 5000), ("乙章", "乙" * 5000)])
        import app.services.facts_builder as fb

        async def _facts(*_a, **_kw):
            return "开挖深度 5.6m"

        monkeypatch.setattr(fb, "build_facts_text", _facts)
        prompts: list[str] = []

        async def _fake_collect(messages, _v, **_kw):
            prompts.append(messages[0]["content"])
            _n = len(prompts)
            return ({"score": 90 - (_n - 1) * 10, "issues": [{"topic": f"批{_n}", "severity": "low"}]}, None)

        monkeypatch.setattr(compliance_mod, "collect_json_response", _fake_collect)
        out = await compliance_mod.run_consistency_audit(sid, db)

        assert out["batch_count"] == len(prompts) == 2
        assert out["partial"] is False
        # 多批取最小分（保守），issue 去重并集
        assert out["score"] == 80
        assert len(out["issues"]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
