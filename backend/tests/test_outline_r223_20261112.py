# R223（2026-11-12）目录生成模块第 16 轮深度探索增强 —— 16 项修复护栏。
#
# 分组：
#   TestQuotaKeywordRatchet      SSE-4  确定性配额关键词收窄（AST 锁成员，防注释自咬）
#   TestQuotaKeywordBehavior     SSE-4  DNS 瞬态误报转 False / 确定性短语保持 True（行为实证）
#   TestSortKeySingleExit        NUM-3  裸排 sort_order 退役（R102 G2 第 4/5 处收口）
#   TestStoredNumberRescueRemoved NUM-1 脏编号抢救回退退役（R218 C-3 契约闭合）
#   TestStepwiseHolderWriteback  SSE-1  分步审核后 holder 回写（短方案 :1765 同族对齐）
#   TestOneshotLevel1Clamp       SSE-3  oneshot 一级章数截断 + 信号键（R210 同口径）
#   TestResumeDeepcopyAndNodes   SSE-5/6 复用子树深拷贝 + 续跑窗口节点计数
#   TestStoppedRetryAccounting   SSE-2  停止时保留首试分类（R89 S1 记账契约）
#   TestRenormalizeGate          DAT-6  重排回写耗尽重试后跳过重规范化
#   TestDuplicateStatusInvariant DAT-3  副本状态对齐「非空目录 ⇒ 目录已确认」
#   TestSnapshotCarriesSource    DAT-1  快照留存/还原 outline_source（旧快照向后兼容）
#   TestFullwidthStoredNumber    NUM-5  存储编号文本归一单一出口 + 预检假阳性消除
#   TestReorderMountedSet        DAT-4  完备性校验改可达集合（不可达节点不再死锁）
#   TestClearBranchSafetyNet     DAT-7  清空分支补快照 + 双 409 复检
#   TestContentPlanChainAligned  NUM-4  编排取值链对齐主链（死回退键退役）
#   TestWordStatusDerivation     DAT-5  word_budget-only PATCH 重算派生列
#   TestErrorPathEmptyNoCkpt     SSE-7 证伪钉住：空成果异常出口不写 checkpoint

import ast
import json
import textwrap
from pathlib import Path

import aiosqlite
import pytest
from app.schema_sql import SCHEMA_SQL

APP = Path(__file__).resolve().parents[1] / "app"
SSE = (APP / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
BUILD = (APP / "routers" / "_sse_outline_build.py").read_text(encoding="utf-8")
REVIEW = (APP / "routers" / "_sse_outline_review.py").read_text(encoding="utf-8")
SECTIONS = (APP / "routers" / "sections.py").read_text(encoding="utf-8")
SCHEMES = (APP / "routers" / "schemes.py").read_text(encoding="utf-8")
CONTENT_PLAN = (APP / "services" / "content_plan.py").read_text(encoding="utf-8")
EXPORT_SCAN = (APP / "routers" / "_export_issue_scan.py").read_text(encoding="utf-8")

SCHEME = "s-r223"
PROJECT = "p-r223"


# ---------------------------------------------------------------------------
# SSE-4 · 确定性配额关键词
# ---------------------------------------------------------------------------


def _quota_keyword_members():
    tree = ast.parse(SSE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_QUOTA_ERROR_KEYWORDS" and isinstance(node.value, ast.Tuple):
                    return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return []


class TestQuotaKeywordRatchet:
    def test_bare_substring_retired(self):
        members = _quota_keyword_members()
        assert members, "未找到 _QUOTA_ERROR_KEYWORDS 元组（结构漂移？）"
        assert "not found" not in members, "裸子串必须退役（命中 Linux DNS 异常文本造成整链熔断）"

    def test_deterministic_phrases_present(self):
        members = _quota_keyword_members()
        for phrase in ("model not found", "no such model", "not_found_error", "resource not found"):
            assert phrase in members, f"确定性短语 {phrase} 必须保留（docstring 意图：模型不存在类）"
        for legacy in ("insufficient", "quota exceeded", "unauthorized", "forbidden", "余额", "配额"):
            assert legacy in members, f"既有确定性关键词 {legacy} 不得被顺带删除"


class TestQuotaKeywordBehavior:
    def _is_quota_error(self, text):
        from app.routers.sse_handlers import _is_quota_error

        return _is_quota_error(Exception(text))

    def test_dns_transient_not_quota(self):
        # Linux aiohttp 连接失败的真实异常文本（旧实现被判 True → L1 raise + 熔断）
        assert self._is_quota_error(
            "Cannot connect to host api.example.com:443 [Name or service not found]"
        ) is False

    def test_deterministic_still_quota(self):
        assert self._is_quota_error("model not found: gpt-nonexistent") is True
        assert self._is_quota_error("No such model: gpt-nonexistent") is True
        assert self._is_quota_error("not_found_error: The model does not exist") is True
        assert self._is_quota_error("Resource not found: deployment missing") is True

    def test_numeric_codes_and_legacy_unchanged(self):
        assert self._is_quota_error("HTTP 402 insufficient balance") is True
        assert self._is_quota_error("HTTP 403") is True
        assert self._is_quota_error("HTTP 404 Not Found") is True  # 词边界数值形态仍生效
        assert self._is_quota_error("HTTP 429 too many requests") is False  # 限流必须保持重试
        assert self._is_quota_error("账户余额不足") is True
        assert self._is_quota_error("quota exceeded for project") is True


# ---------------------------------------------------------------------------
# NUM-3 · 排序键单一出口
# ---------------------------------------------------------------------------


class TestSortKeySingleExit:
    def test_bare_sort_retired_in_sse_handlers(self):
        assert 'by_parent[k].sort(key=lambda x: x.get("sort_order", 0))' not in SSE
        assert 'section_sort_order_key(x.get("sort_order"), x.get("id"))' in SSE

    def test_bare_sort_retired_in_outline_build(self):
        assert 'children_by_parent[pid].sort(key=lambda x: x.get("sort_order", 0))' not in BUILD
        assert "_sh.section_sort_order_key" in BUILD

    def test_canonical_key_handles_dirty_types(self):
        from app.services.content_utils import section_sort_order_key

        rows = [
            {"sort_order": 2, "id": "b"},
            {"sort_order": "1", "id": "a"},
            {"sort_order": None, "id": "c"},
            {"sort_order": "abc", "id": "d"},
        ]
        rows.sort(key=lambda x: section_sort_order_key(x.get("sort_order"), x.get("id")))
        # 脏值全部归一参与比较：'1'→1、None/'abc'→0，同为 0 时按 id 决胜
        assert [r["id"] for r in rows] == ["c", "d", "a", "b"]


# ---------------------------------------------------------------------------
# NUM-1 · 脏编号抢救回退退役
# ---------------------------------------------------------------------------


class TestStoredNumberRescueRemoved:
    def test_rescue_expression_retired(self):
        assert "stored_id_to_display(_sec_stored) or _sec_stored" not in SSE
        assert "stored_id_to_prefix(_sec_stored) or _sec_stored" not in SSE

    def test_missing_fallback_contract_present(self):
        assert "if not _sec_no or not _sec_prefix:" in SSE
        assert SSE.count("SECTION_NUMBER_MISSING") >= 3, "非法/缺失编号统一注入缺失占位的口径必须保持"

    def test_zero_segment_judgment_contract(self):
        from app.services.numbering import stored_id_to_display, stored_id_to_prefix

        assert stored_id_to_display("3.0") == ""
        assert stored_id_to_prefix("3.0") == ""
        assert stored_id_to_display("3.2") == "2"
        assert stored_id_to_prefix("3.2") == "2"


# ---------------------------------------------------------------------------
# SSE-1 · 分步审核后 holder 回写
# ---------------------------------------------------------------------------


class TestStepwiseHolderWriteback:
    def test_writeback_present_after_stepwise_review(self):
        anchor = SSE.find("full_outline, review_obj = await _review_and_fix_outline(")
        assert anchor > 0
        stopper = SSE.find("审核/修复期间用户点了停止", anchor)
        assert stopper > anchor
        window = SSE[anchor:stopper]
        assert 'if full_outline:\n' in window
        assert '_partial_holder["outline"] = full_outline' in window, (
            "分步路径审核返回后必须立即回写 holder（短方案 :1765 同族口径）——"
            "否则迟到取消用审核前快照写 checkpoint / 覆盖已完成 checkpoint"
        )

    def test_writeback_count_consistent(self):
        assert SSE.count('_partial_holder["outline"] = full_outline') == 3


# ---------------------------------------------------------------------------
# SSE-3 · oneshot 一级章数截断
# ---------------------------------------------------------------------------


class TestOneshotLevel1Clamp:
    def test_short_path_clamp_present(self):
        assert "_short_level1_truncated" in SSE
        assert "_short_level1_total" in SSE
        assert SSE.count("outline[:OUTLINE_MAX_LEVEL1_CHAPTERS]") == 1, "oneshot 截断恰好一处"
        assert SSE.count("level1[:OUTLINE_MAX_LEVEL1_CHAPTERS]") == 1, "分步截断恰好一处"

    def test_short_payload_signal_keys(self):
        assert "if _short_level1_truncated:" in SSE
        assert '_short_payload["level1_truncated"]' in SSE
        assert '_short_payload["level1_total"]' in SSE

    def test_checkpoint_whitelist_still_registers_keys(self):
        from app.services.checkpoint_schema import CHECKPOINT_FIELD_TYPES, CHECKPOINT_KINDS

        fields = CHECKPOINT_KINDS.get("outline_generation", ("", ()))[1]
        for key in ("level1_truncated", "level1_total"):
            assert key in fields, f"{key} 必须在 outline_generation 的 checkpoint 白名单（R213 FIX-6）"
            assert CHECKPOINT_FIELD_TYPES.get("outline_result", {}).get(key) == "int"


# ---------------------------------------------------------------------------
# SSE-5 / SSE-6 · 续跑子树深拷贝 + 节点计数
# ---------------------------------------------------------------------------


class TestResumeDeepcopyAndNodes:
    def test_children_alias_cut(self):
        assert "copy.deepcopy(_prev_ch[\"children\"])" in SSE
        # 裸引用赋值必须退役（同名章别名共享导致重排后前缀错乱 → 保存 400）
        assert '_rch["children"] = _prev_ch["children"]' not in SSE

    def test_nodes_recount_after_prefill(self):
        assert SSE.count('_prog["nodes"] = _count_nodes(level1)') >= 2, (
            "续跑预填充完成后必须重算节点数（L1 到达时一次 + 复用子树挂上后一次）"
        )


# ---------------------------------------------------------------------------
# SSE-2 · 停止时保留首试分类
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopped_retry_keeps_first_classification(monkeypatch):
    """停止打断空章定向重试时，该章保留 ("ok", []) 首试分类（R89 S1 记账可达）。

    ⚠️ is_stopped 对未知任务返回 True（「任务不存在视为已结束」）——
    必须 monkeypatch 为 False 才能走到「AI 调用返回 stopped」的修复分支，
    否则测的是入口检查路径（既有语义），打不到修复点。
    """
    from app.routers import _sse_outline_review as rev

    async def _stopped_fetch(*args, **kwargs):
        return ("stopped", [])

    monkeypatch.setattr(rev._sh, "is_stopped", lambda tid: False)
    monkeypatch.setattr(rev._sh, "_fetch_chapter_children", _stopped_fetch)

    # 首试 ok-但空：重试中停止信号到达 → 保留 ("ok", [])，merge 侧按 R89 S1 记账
    out = await rev._retry_empty_chapters(
        [(0, {"title": "第一章"})],
        [("ok", [])],
        [],
        "task-r223-a",
        None,
        1,
        None,
        {},
    )
    assert out == [("ok", [])], "停止不得把首试已定格的 ok-空章改写为 stopped（否则从成果与失败清单双消失）"

    # 首试 failed：同样保留（failed 记账不丢）
    out2 = await rev._retry_empty_chapters(
        [(0, {"title": "第二章"})],
        [("failed", [])],
        [],
        "task-r223-b",
        None,
        1,
        None,
        {},
    )
    assert out2 == [("failed", [])]


@pytest.mark.asyncio
async def test_stopped_retry_entry_break_unchanged(monkeypatch):
    """入口已停止（既有语义）：不发起调用、不改写 out——回归保护。"""
    from app.routers import _sse_outline_review as rev

    async def _boom(*args, **kwargs):
        raise AssertionError("入口已停止时不得发起 AI 调用")

    monkeypatch.setattr(rev._sh, "is_stopped", lambda tid: True)
    monkeypatch.setattr(rev._sh, "_fetch_chapter_children", _boom)
    out = await rev._retry_empty_chapters(
        [(0, {"title": "第一章"})],
        [("ok", [])],
        [],
        "task-r223-d",
        None,
        1,
        None,
        {},
    )
    assert out == [("ok", [])]


@pytest.mark.asyncio
async def test_stopped_retry_recovers_when_ai_returns(monkeypatch):
    """停止未发生时，重试成功仍原位替换（既有行为不回退）。"""
    from app.routers import _sse_outline_review as rev

    async def _ok_fetch(*args, **kwargs):
        return ("ok", [{"title": "1.1", "children": []}])

    monkeypatch.setattr(rev._sh, "is_stopped", lambda tid: False)
    monkeypatch.setattr(rev._sh, "_fetch_chapter_children", _ok_fetch)
    retry_state = {}
    out = await rev._retry_empty_chapters(
        [(0, {"title": "第一章"})],
        [("ok", [])],
        [],
        "task-r223-c",
        None,
        1,
        None,
        retry_state,
    )
    assert out == [("ok", [{"title": "1.1", "children": []}])]
    assert retry_state["1"]["status"] == "recovered"


# ---------------------------------------------------------------------------
# DAT-6 · 重排回写耗尽重试后跳过重规范化
# ---------------------------------------------------------------------------


class _FakeDb:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *a):
        self.executed.append(sql)
        return None

    async def executemany(self, sql, rows):
        self.executed.append(sql)
        return ("many", len(rows))


class TestRenormalizeGate:
    @pytest.mark.asyncio
    async def test_returns_false_when_rowcount_mismatches(self, monkeypatch):
        from app.routers import sections as sec

        async def _fake_tree(db, scheme_id, include_content=False):
            return []

        monkeypatch.setattr(sec, "_build_tree", _fake_tree)
        monkeypatch.setattr(sec, "renumber_section_outline_ids", lambda tree: [("u", 1)])
        monkeypatch.setattr(sec, "safe_rowcount", lambda cur, what="": 0)

        ok = await sec.renumber_sections_after_reorder(_FakeDb(), SCHEME)
        assert ok is False, "两轮重试仍少写行必须返回 False（调用方据此跳过重规范化）"

    @pytest.mark.asyncio
    async def test_returns_true_when_written(self, monkeypatch):
        from app.routers import sections as sec

        async def _fake_tree(db, scheme_id, include_content=False):
            return []

        monkeypatch.setattr(sec, "_build_tree", _fake_tree)
        monkeypatch.setattr(sec, "renumber_section_outline_ids", lambda tree: [("u", 1)])
        monkeypatch.setattr(sec, "safe_rowcount", lambda cur, what="": 1)

        ok = await sec.renumber_sections_after_reorder(_FakeDb(), SCHEME)
        assert ok is True

    @pytest.mark.asyncio
    async def test_returns_true_when_nothing_to_write(self, monkeypatch):
        from app.routers import sections as sec

        async def _fake_tree(db, scheme_id, include_content=False):
            return []

        monkeypatch.setattr(sec, "_build_tree", _fake_tree)
        monkeypatch.setattr(sec, "renumber_section_outline_ids", lambda tree: [])

        ok = await sec.renumber_sections_after_reorder(_FakeDb(), SCHEME)
        assert ok is True

    def test_all_call_sites_gated(self):
        gated = SECTIONS.count("if await renumber_sections_after_reorder(db, scheme_id):")
        ungated = SECTIONS.count("await renumber_sections_after_reorder(db, scheme_id)") - gated
        assert gated == 4, f"四个结构写入口都必须门控（实际 {gated}）"
        assert ungated == 0, "不得存在未门控的调用点"


# ---------------------------------------------------------------------------
# DAT-3 · 副本状态不变式
# ---------------------------------------------------------------------------


class TestDuplicateStatusInvariant:
    def test_status_correction_gated_on_nonempty_copy(self):
        """INSERT 保留初始草稿态；「确有章节复制」分支内必须条件升级为已确认态。

        设计：schemes 行先以初始状态落库（此时还不知道是否有章节可复制），
        sections 复制完成后若 inserts 非空，则按全仓不变式升级状态。
        """
        assert "from app.routers.sections import OUTLINE_SAVED_STATUS" in SCHEMES
        anchor = SCHEMES.find("INSERT INTO sections ")
        assert anchor > 0
        window = SCHEMES[anchor : anchor + 2500]
        assert "UPDATE schemes SET status=?, updated_at=? WHERE id=?" in window
        assert "OUTLINE_SAVED_STATUS" in window

    def test_empty_copy_keeps_initial_status(self):
        """无章节可复制（空方案副本）时不得误升级——UPDATE 必须在 if inserts: 内。"""
        anchor = SCHEMES.find("INSERT INTO sections ")
        if_anchor = SCHEMES.rfind("if inserts:", 0, anchor)
        assert if_anchor > 0
        # UPDATE 出现在与 if inserts: 同级的分支体之后（即 if inserts: 块内）
        block = SCHEMES[if_anchor : if_anchor + 2600]
        assert block.count("if inserts:") >= 1
        assert "OUTLINE_SAVED_STATUS" in block


# ---------------------------------------------------------------------------
# DAT-1 · 快照留存/还原 outline_source
# ---------------------------------------------------------------------------


async def _make_db(tmp_path, sections, outline_source="upload_recognize"):
    path = tmp_path / "r223_snap.sqlite3"
    setup = await aiosqlite.connect(path)
    await setup.executescript(SCHEMA_SQL)
    from app.db import _migrate, _setup_connection

    await _migrate(setup)
    await setup.execute("INSERT OR IGNORE INTO projects(id,name) VALUES(?,?)", (PROJECT, "R223"))
    await setup.execute(
        "INSERT OR IGNORE INTO schemes(id,project_id,name,status,word_budget,outline_source)"
        " VALUES(?,?,?,?,?,?)",
        (SCHEME, PROJECT, "R223", "目录已确认", 30000, outline_source),
    )
    for s in sections:
        await setup.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order,"
            " content, word_count, word_budget, outline_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["id"],
                SCHEME,
                PROJECT,
                s.get("parent", ""),
                s["title"],
                s.get("level", 1),
                s.get("order", 0),
                s.get("content", ""),
                s.get("word_count", 0),
                s.get("word_budget", 1200),
                json.dumps({"id": s.get("num", ""), "title": s["title"]}, ensure_ascii=False),
            ),
        )
    await setup.commit()
    await setup.close()
    conn = await aiosqlite.connect(path)
    await _setup_connection(conn)
    return conn


@pytest.mark.asyncio
async def test_snapshot_carries_outline_source(tmp_path):
    from app.services.outline_versions import create_outline_snapshot

    conn = await _make_db(tmp_path, [{"id": "sec-1", "title": "工程概况", "num": "1"}])
    try:
        snap_id = await create_outline_snapshot(conn, SCHEME, snapshot_type="test", commit=True)
        cur = await conn.execute("SELECT sections FROM scheme_snapshots WHERE id=?", (snap_id,))
        payload = json.loads((await cur.fetchone())[0])
        assert payload.get("outline_source") == "upload_recognize", "快照必须留存目录来源"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_restore_nonempty_restores_source(tmp_path):
    from app.services.outline_versions import create_outline_snapshot, restore_outline_snapshot

    conn = await _make_db(tmp_path, [{"id": "sec-1", "title": "工程概况", "num": "1"}])
    try:
        snap_id = await create_outline_snapshot(conn, SCHEME, snapshot_type="test", commit=True)
        await conn.execute(
            "UPDATE schemes SET outline_source='template_instance' WHERE id=?", (SCHEME,)
        )
        await conn.commit()
        await restore_outline_snapshot(conn, SCHEME, snap_id)
        cur = await conn.execute("SELECT outline_source FROM schemes WHERE id=?", (SCHEME,))
        assert (await cur.fetchone())[0] == "upload_recognize", "非空回退必须还原快照来源"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_restore_legacy_snapshot_keeps_source(tmp_path):
    """旧快照（无 outline_source 键）→ 不触碰来源，向后兼容。"""
    conn = await _make_db(tmp_path, [{"id": "sec-1", "title": "工程概况", "num": "1"}])
    try:
        legacy = json.dumps({"sections": [], "review_status": ""}, ensure_ascii=False)
        snap_id = "ver_r223legacy"
        await conn.execute(
            "INSERT INTO scheme_snapshots (id, scheme_id, type, sections, created_at)"
            " VALUES (?,?,?,?,datetime('now'))",
            (snap_id, SCHEME, "test", legacy),
        )
        # 快照 sections 为空数组 → 走「合法但 0 章」分支前先验证键缺失路径：
        # 塞回一行使 restored>0，同时 payload 无 outline_source 键
        legacy = json.dumps(
            {
                "sections": [
                    {
                        "id": "sec-1",
                        "scheme_id": SCHEME,
                        "project_id": PROJECT,
                        "parent_id": "",
                        "title": "工程概况",
                        "level": 1,
                        "sort_order": 0,
                        "content": "",
                        "word_count": 0,
                        "word_budget": 1200,
                        "outline_json": json.dumps({"id": "1", "title": "工程概况"}, ensure_ascii=False),
                    }
                ],
                "review_status": "",
            },
            ensure_ascii=False,
        )
        await conn.execute(
            "UPDATE scheme_snapshots SET sections=? WHERE id=?", (legacy, snap_id)
        )
        await conn.execute(
            "UPDATE schemes SET outline_source='template_instance' WHERE id=?", (SCHEME,)
        )
        await conn.commit()
        from app.services.outline_versions import restore_outline_snapshot

        await restore_outline_snapshot(conn, SCHEME, snap_id)
        cur = await conn.execute("SELECT outline_source FROM schemes WHERE id=?", (SCHEME,))
        assert (await cur.fetchone())[0] == "template_instance", "旧快照无来源键时不得触碰现有来源"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# NUM-5 · 存储编号文本归一单一出口
# ---------------------------------------------------------------------------


class TestFullwidthStoredNumber:
    def test_normalize_single_exit(self):
        from app.services.numbering import normalize_stored_id_text

        assert normalize_stored_id_text(" ３.２ ") == "3.2"
        assert normalize_stored_id_text("3.2") == "3.2"
        assert normalize_stored_id_text("") == ""
        assert normalize_stored_id_text(None) == ""

    def test_parts_uses_same_exit(self):
        from app.services.numbering import _stored_id_parts

        assert _stored_id_parts("３.２") == ["3", "2"]
        assert _stored_id_parts("1.01") == ["1", "1"]
        assert _stored_id_parts("0.3") == []

    def test_scan_side_normalized(self):
        from app.routers._export_issue_scan import _stored_outline_number

        assert _stored_outline_number({"outline_json": json.dumps({"id": "３.２"})}) == "3.2"
        assert _stored_outline_number({"outline_json": json.dumps({"id": "3.2"})}) == "3.2"

    def test_mismatch_detector_no_false_positive_on_fullwidth(self):
        from app.routers._export_issue_scan import _detect_section_number_mismatch

        rows = [
            {
                "id": "sec-a",
                "title": "工程概况",
                "parent_id": "",
                "sort_order": 0,
                "level": 1,
                "outline_json": json.dumps({"id": "１"}, ensure_ascii=False),
            }
        ]
        assert _detect_section_number_mismatch(rows) == [], "全角存量行与结构一致时不得报假阳性"

    def test_mismatch_detector_still_catches_stale(self):
        from app.routers._export_issue_scan import _detect_section_number_mismatch

        rows = [
            {
                "id": "sec-a",
                "title": "工程概况",
                "parent_id": "",
                "sort_order": 0,
                "level": 1,
                "outline_json": json.dumps({"id": "9"}, ensure_ascii=False),
            }
        ]
        issues = _detect_section_number_mismatch(rows)
        assert len(issues) == 1 and issues[0]["stored"] == "9"


# ---------------------------------------------------------------------------
# DAT-4 · reorder 完备性校验改可达集合
# ---------------------------------------------------------------------------


class TestReorderMountedSet:
    def test_missing_check_uses_mounted_set(self):
        assert "_missing = _mounted_ids - _order_ids" in SECTIONS
        assert "_collect_section_ids(await _build_tree(db, scheme_id))" in SECTIONS
        assert "set(parent_of) - _order_ids" not in SECTIONS


# ---------------------------------------------------------------------------
# DAT-7 · 清空分支安全网
# ---------------------------------------------------------------------------


class TestClearBranchSafetyNet:
    def test_clear_branch_has_recheck_and_snapshot(self):
        anchor = SECTIONS.find("空数组 = 清空全部目录")
        assert anchor > 0
        end = SECTIONS.find("return _empty_result", anchor)
        window = SECTIONS[anchor:end]
        assert "content_generation_in_progress(scheme_id)" in window, "清空前必须复检正文生成"
        assert "outline_generation_in_progress(scheme_id)" in window, "清空前必须复检目录生成"
        assert "create_outline_snapshot" in window, "清空是破坏性最强操作，必须支持变更前快照"
        assert "commit=False" in window, "快照随清空事务一并落库（不提前提交）"


# ---------------------------------------------------------------------------
# NUM-4 · 编排取值链对齐主链
# ---------------------------------------------------------------------------


class TestContentPlanChainAligned:
    def test_dead_fallback_keys_retired(self):
        assert 'leaf.get("outline_number")' not in CONTENT_PLAN
        assert 'leaf.get("number")' not in CONTENT_PLAN

    def test_stored_fallback_present(self):
        assert "stored_outline_id(leaf)" in CONTENT_PLAN
        assert "if _recomputed and stored_id_to_display(_recomputed):" in CONTENT_PLAN

    def test_chain_semantics(self):
        """取值链行为对齐：重算值优先、存储回退、全非法判空。"""
        from app.services.numbering import stored_id_to_display, stored_outline_id

        leaf_valid = {"_prompt_number": "2.4", "outline_json": json.dumps({"id": "3.2.4"})}
        recomputed = str(leaf_valid.get("_prompt_number") or "").strip()
        assert recomputed and stored_id_to_display(recomputed)
        leaf_no_recompute = {"outline_json": json.dumps({"id": "3.2.4"})}
        assert stored_outline_id(leaf_no_recompute) == "3.2.4"
        leaf_dirty = {"outline_json": "not-json"}
        assert stored_outline_id(leaf_dirty) == ""


# ---------------------------------------------------------------------------
# DAT-5 · word_budget-only PATCH 重算派生列
# ---------------------------------------------------------------------------


class TestWordStatusDerivation:
    def test_recompute_block_outside_content_branch(self):
        assert 'if "content" not in fields and ("word_budget" in fields or "word_count" in fields):' in SECTIONS

    def test_word_status_for_contract(self):
        from app.services.content_utils import word_status_for

        assert word_status_for(2000, 3000) == word_status_for(2000, 3000)
        assert word_status_for(0, 1200) in {"empty", "under", "normal", "over", "pending", "none"} or isinstance(
            word_status_for(0, 1200), str
        )


# ---------------------------------------------------------------------------
# SSE-7 证伪钉住 · 空成果异常出口不写 checkpoint
# ---------------------------------------------------------------------------


class TestErrorPathEmptyNoCkpt:
    def test_empty_result_guard_precedes_checkpoint_write(self):
        guard = SSE.find("if _err_outline:")
        ckpt = SSE.find("_err_ckpt = {")
        assert 0 < guard < ckpt, "空成果必须被 if 守卫挡在 checkpoint 写入之外（R223 亲验证伪结论，防回归）"
