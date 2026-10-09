"""审核与预检 · 问题定向自动修复（2026-09-30）

守护新链路「问题 → 定位矛盾位置 → AI 改写 → 校验 → 落库（可回滚）」的关键不变量：

1. **能力表是唯一事实源**：前端按钮状态来自后端 ``finding.autofix``；
   ``manual`` 必须给出可执行的下一步；派生编号（CON-05-1）必须归一到基规则。
2. **定位不到就拒绝盲改**：``not_located`` 下正文必须**逐字不变**。
3. **失败绝不写坏正文**：AI 返回空 / 内容未变 / 结构被破坏 / 要点缺失
   → 判 failed 且不落库；成功才写库、重算字数、后退审核状态。
"""
import json

from app.services import review_autofix
from app.services.content_utils import text_word_count, word_status_for

SCHEME = "sch-1"
PROJ = "prj-1"
SEC_A = "sec-a"
SEC_B = "sec-b"

_BEFORE_A = "本工程总工期为 120 日历天，混凝土强度等级为 C30。\n第二段说明施工部署。\n"
_AFTER_A = "本工程总工期为 90 日历天，混凝土强度等级为 C30。\n第二段说明施工部署。\n"


def _all_rule_ids() -> list[str]:
    from app.services.audit_rules import active_rules
    return [r.rule_id for r in active_rules()]


async def _seed(db, *, a_content: str = _BEFORE_A, b_content: str = "") -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PROJ, "P"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (SCHEME, PROJ, "深基坑专项方案"))
    for sid, title, content, order in (
            (SEC_A, "工程概况", a_content, 0),
            (SEC_B, "施工进度计划", b_content, 1)):
        wc = text_word_count(content)
        await db.execute(
            "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
            "word_status,word_budget,status,level,sort_order) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, SCHEME, PROJ, title, content, wc,
             word_status_for(wc, 1500), 1500, "generated", 1, order))
    await db.commit()


def _sections(a_content: str = _BEFORE_A, b_content: str = "") -> list[dict]:
    return [
        {"id": SEC_A, "title": "工程概况", "content": a_content},
        {"id": SEC_B, "title": "施工进度计划", "content": b_content},
    ]


def _con01_finding() -> dict:
    """CON-01 的真实 evidence 形态：``值（章节标题）``（preflight_engine 产出）。"""
    return {
        "rule_id": "CON-01", "dimension": "consistency", "severity": "high",
        "title": "全文一致性", "detail": "「工期」在正文中出现 2 种不同取值",
        "evidence": ["120日历天（工程概况）", "90日历天（施工进度计划）"],
        "section_id": "", "section_title": "", "suggestion": "请统一工期口径",
        "basis": "", "mode": "program",
    }


class TestCapabilityTable:
    """能力表：唯一事实源 + 契约完整性。"""

    def test_derived_rule_id_resolves_to_base_rule(self):
        assert review_autofix.capability_of("CON-05-1").mode == \
            review_autofix.capability_of("CON-05").mode == "manual"

    def test_auto_mode_rules_never_call_ai(self):
        """DLV-05/06/07 是确定性问题：必须走程序化，绝不能调 AI。"""
        for rid in ("DLV-05", "DLV-06", "DLV-07"):
            assert review_autofix.capability_of(rid).mode == "auto"

    def test_manual_mode_always_gives_actionable_reason(self):
        """manual 必须说清「去哪处理」，不能只说"不支持"。"""
        for rule in _all_rule_ids():
            cap = review_autofix.capability_of(rule)
            if cap.mode == "manual":
                assert cap.reason, f"{rule} 声明为 manual 却没有给出替代路径"
                assert len(cap.reason) >= 10, f"{rule} 的替代路径过于简略"

    def test_unknown_rule_falls_back_without_silencing(self):
        cap = review_autofix.capability_of("ZZZ-99")
        assert cap.mode == "manual" and cap.reason

    def test_ai_mode_rules_have_instruction_and_anchor(self):
        """ai 模式必须给出整改要求 + 定位方式，否则 AI 只会自由发挥。"""
        for rule in _all_rule_ids():
            cap = review_autofix.capability_of(rule)
            if cap.mode == "ai":
                assert cap.instruction, f"{rule} 为 ai 模式但未给出整改要求"
                assert cap.anchor, f"{rule} 为 ai 模式但未声明定位方式"

    def test_std01_forbids_all_abolished_codes(self):
        """STD-01 的 must_not_contain 必须覆盖全部废止编号（与判据同源）。"""
        from app.services.standards_registry import ABOLISHED_STANDARDS
        cap = review_autofix.capability_of("STD-01")
        for code in ABOLISHED_STANDARDS:
            assert code in cap.must_not_contain

    def test_capability_summary_only_adds_field(self):
        """capability_summary 只能新增 autofix 字段，不得改动既有字段。"""
        finding = {"rule_id": "CON-01", "severity": "high", "detail": "x"}
        before = dict(finding)
        review_autofix.capability_summary([finding])
        assert finding["autofix"]["fixable"] is True
        for k, v in before.items():
            assert finding[k] == v, f"capability_summary 篡改了既有字段 {k}"

    def test_capability_summary_tolerates_non_dict(self):
        review_autofix.capability_summary([None, "x", {"rule_id": "DLV-05"}])


class TestLocateTargets:
    """定位矛盾位置：本需求的核心（必须具体到行）。"""

    def test_value_anchor_locates_each_occurrence_with_line(self):
        targets = review_autofix.locate_targets(_con01_finding(), _sections())
        assert targets, "必须定位到具体位置"
        assert "120日历天" in {t["value"] for t in targets}
        for t in targets:
            assert t["section_id"] in (SEC_A, SEC_B)
            assert t["line"] >= 1
            assert t["context"] and t["why"]

    def test_value_anchor_line_number_is_accurate(self):
        f = _con01_finding()
        f["evidence"] = ["120日历天（工程概况）"]
        targets = review_autofix.locate_targets(
            f, _sections(a_content="第一行无关内容\n第二行写明总工期为 120 日历天。\n"))
        assert len(targets) == 1
        assert targets[0]["line"] == 2, targets[0]
        assert "总工期" in targets[0]["context"]

    def test_section_anchor_targets_whole_section(self):
        targets = review_autofix.locate_targets(
            {"rule_id": "CMP-09", "section_id": SEC_A, "evidence": []}, _sections())
        assert len(targets) == 1
        assert targets[0]["section_id"] == SEC_A
        assert "整章" in targets[0]["why"]

    def test_section_anchor_without_section_id_locates_nothing(self):
        """补充类问题若 finding 未带 section_id → 定位不到（拒绝盲改）。"""
        f = {"rule_id": "CMP-09", "section_id": "", "evidence": []}
        assert review_autofix.locate_targets(f, _sections()) == []

    def test_ctrl_anchor_finds_control_char(self):
        targets = review_autofix.locate_targets(
            {"rule_id": "DLV-05", "section_id": "", "evidence": []},
            _sections(a_content="正常内容\x07异常内容"))
        assert len(targets) == 1 and targets[0]["section_id"] == SEC_A

    def test_fence_anchor_finds_unclosed_fence(self):
        targets = review_autofix.locate_targets(
            {"rule_id": "DLV-07", "section_id": "", "evidence": []},
            _sections(a_content="正文\n```mermaid\ngraph TD; A-->B\n"))
        assert len(targets) == 1 and "未闭合" in targets[0]["matched"]

    def test_standard_anchor_locates_abolished_code(self):
        targets = review_autofix.locate_targets(
            {"rule_id": "STD-01", "section_id": "", "evidence": ["GB 50202-2002"]},
            _sections(a_content="地基验收依据 GB 50202-2002 执行。"))
        assert len(targets) == 1 and targets[0]["value"] == "GB 50202-2002"

    def test_not_locatable_returns_empty(self):
        """evidence 里的取值在正文中不存在 → 定位不到（而非乱指一处）。"""
        f = _con01_finding()
        f["evidence"] = ["999日历天（工程概况）"]
        assert review_autofix.locate_targets(f, _sections()) == []


class TestProgrammaticFix:
    """auto 模式：确定性修复，不调 AI。"""

    def test_ctrl_removed(self):
        cap = review_autofix.capability_of("DLV-05")
        out, problems = review_autofix.fix_programmatic(cap, "正常\x07内容\x00")
        assert problems == []
        assert "\x07" not in out and "\x00" not in out
        assert "正常" in out and "内容" in out

    def test_fence_closed(self):
        cap = review_autofix.capability_of("DLV-07")
        out, problems = review_autofix.fix_programmatic(
            cap, "正文\n```mermaid\ngraph TD; A-->B\n")
        assert problems == [] and out.count("```") % 2 == 0

    def test_no_issue_reports_reason(self):
        """无需修复时必须给出原因，不能静默返回「成功」。"""
        _out, problems = review_autofix.fix_programmatic(
            review_autofix.capability_of("DLV-05"), "干净正文")
        assert problems


class TestValidateFixed:
    """校验器：失败即不写库。"""

    def test_empty_after_fails(self):
        ok, problems = review_autofix.validate_fixed(
            "原文", "  ", review_autofix.capability_of("CMP-09"))
        assert not ok and problems

    def test_unchanged_fails(self):
        ok, _ = review_autofix.validate_fixed(
            "原文内容", "原文内容", review_autofix.capability_of("CMP-09"))
        assert not ok

    def test_structure_break_fails(self):
        """标题被删光 → 结构破坏，必须拒绝（否则导出成稿丢章节）。"""
        before = "# 1 计算书\n\n## 1.1 依据\n\n内容一段。\n\n## 1.2 过程\n\n内容二段。\n"
        after = "只有一段纯文本内容，与原文结构完全不同且不满足必需要点。"
        ok, problems = review_autofix.validate_fixed(
            before, after, review_autofix.capability_of("CMP-09"))
        assert not ok and any("标题" in p for p in problems)

    def test_must_contain_missing_fails(self):
        """语义校验：结构没坏但没补上要点 → 同样判失败。"""
        ok, problems = review_autofix.validate_fixed(
            "本章仅有少量文字说明，没有给出任何计算过程。",
            "本章说明了计算的重要性，请相关人员予以重视并按规范执行。",
            review_autofix.capability_of("CMP-09"))
        assert not ok and any("计算依据" in p for p in problems)

    def test_must_not_contain_residual_fails(self):
        """废止编号仍在正文 → 判失败（AI 换成另一个废止编号也算）。"""
        ok, problems = review_autofix.validate_fixed(
            "依据 GB 50202-2002 执行。\n",
            "依据 GB 50202-2002 与 JGJ 59-99 执行。\n",
            review_autofix.capability_of("STD-01"))
        assert not ok and any("GB 50202-2002" in p for p in problems)

    def test_good_fix_passes(self):
        before = "本章说明本工程计算书内容。"
        after = ("本章说明本工程计算书内容。\n\n"
                 "计算依据：JGJ 59-2011；计算过程：按规范公式代入参数验算，"
                 "结论满足安全系数要求。\n")
        ok, problems = review_autofix.validate_fixed(
            before, after, review_autofix.capability_of("CMP-09"))
        assert ok, problems


class TestApplyFixEndToEnd:
    """端到端：真库 + 假 AI。

    ⚠️ 分层（services 不得写正文）：``apply_fix`` 只返回 ``pending``，
    落库由 ``routers/review_autofix._persist_fixed`` 完成 —— 故此处经
    ``_apply_via_router`` 走完整链路（含快照 / 字数 / 审核状态）。
    """

    @staticmethod
    async def _apply_via_router(db, *, finding, sections):
        from app.routers import review_autofix as ra
        res = await review_autofix.apply_fix(
            db, scheme_id=SCHEME, finding=finding, sections=sections,
            scheme={"name": "深基坑专项方案", "type": "深基坑"})
        pending = res.pop("pending", []) or []
        if pending:
            res["snapshot_id"] = await ra._persist_fixed(
                db, scheme_id=SCHEME, rule_id=finding.get("rule_id") or "",
                pending=pending, repair_id=res.get("repair_id", ""))
        return res

    async def test_ai_fix_writes_content_and_resets_review(self, db_conn, monkeypatch):
        await _seed(db_conn)
        # 预置「已通过」：改写正文后必须退回待审核（原结论已失效）
        await db_conn.execute(
            "UPDATE sections SET review_status='approved' WHERE id=?", (SEC_A,))
        await db_conn.commit()

        seen: list = []

        async def fake_chat(messages, **kwargs):
            seen.append(kwargs)
            return _AFTER_A

        monkeypatch.setattr(review_autofix, "chat_with_fallback", fake_chat)
        res = await self._apply_via_router(
            db_conn, finding=_con01_finding(), sections=_sections())

        assert res["ok"], res
        assert res["stats"]["repaired"] == 1
        # AI 必须带 scene 标记（否则统计归空场景、场景路由配不上）
        assert seen and seen[0].get("scene") == "review_autofix"
        cur = await db_conn.execute(
            "SELECT content, word_count, word_status, review_status FROM sections"
            " WHERE id=?", (SEC_A,))
        row = await cur.fetchone()
        assert "90 日历天" in row["content"] and "120 日历天" not in row["content"]
        # 字数用全项目唯一口径重算（不得用 len(content) 含图表代码）
        assert row["word_count"] == text_word_count(row["content"])
        assert row["review_status"] == "pending"
        assert res["snapshot_id"]

    async def test_snapshot_rollback_restores_original(self, db_conn, monkeypatch):
        await _seed(db_conn)

        async def fake_chat(messages, **kwargs):
            return _AFTER_A

        monkeypatch.setattr(review_autofix, "chat_with_fallback", fake_chat)
        res = await self._apply_via_router(
            db_conn, finding=_con01_finding(), sections=_sections())
        assert res["ok"]

        from app.services import repair_record
        rb = await repair_record.rollback_snapshot(
            db_conn, res["snapshot_id"], undo_type="review_autofix_rollback")
        assert rb["restored"] == 1
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())["content"] == _BEFORE_A

    async def test_ai_failure_keeps_original_content(self, db_conn, monkeypatch):
        """AI 抛异常 → 判 failed 且正文逐字不变（绝不写坏正文）。"""
        await _seed(db_conn)

        async def boom(messages, **kwargs):
            raise RuntimeError("provider 不可用")

        monkeypatch.setattr(review_autofix, "chat_with_fallback", boom)
        res = await self._apply_via_router(
            db_conn, finding=_con01_finding(), sections=_sections())

        assert not res["ok"]
        assert res["items"][0]["status"] == "failed"
        # ⚠️ 必须把异常**原因**透出：静默吞掉异常（problems 置空）会让用户只看到
        # 「未修复」而不知道是 AI 服务挂了 —— 排障方向直接丢失。
        assert any("异常" in p or "不可用" in p for p in res["items"][0]["problems"]), \
            res["items"][0]["problems"]
        assert res["snapshot_id"] == ""      # 无改动 → 不产生快照
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())["content"] == _BEFORE_A

    async def test_service_layer_never_writes_content(self, db_conn, monkeypatch):
        """分层护栏：``apply_fix`` 本身**不写** sections.content、不产生快照。"""
        await _seed(db_conn)

        async def fake_chat(messages, **kwargs):
            return _AFTER_A

        monkeypatch.setattr(review_autofix, "chat_with_fallback", fake_chat)
        res = await review_autofix.apply_fix(
            db_conn, scheme_id=SCHEME, finding=_con01_finding(),
            sections=_sections(), scheme={"name": "S", "type": "深基坑"})
        assert res["ok"] and res["snapshot_id"] == ""
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())["content"] == _BEFORE_A

    async def test_invalid_ai_output_keeps_original(self, db_conn, monkeypatch):
        """AI 返回未消除问题的内容（废止编号仍在）→ 不落库。"""
        await _seed(db_conn, a_content="依据 GB 50202-2002 执行。\n")
        f = {"rule_id": "STD-01", "section_id": "", "title": "废止标准",
             "detail": "引用废止标准", "evidence": ["GB 50202-2002"],
             "suggestion": "替换为现行版本"}

        async def fake_chat(messages, **kwargs):
            return "依据 GB 50202-2002 继续执行，同时参照 JGJ 59-99。\n"

        monkeypatch.setattr(review_autofix, "chat_with_fallback", fake_chat)
        res = await self._apply_via_router(db_conn, finding=f, sections=_sections())
        assert not res["ok"]
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert "GB 50202-2002" in (await cur.fetchone())["content"]

    async def test_programmatic_fix_end_to_end_no_ai(self, db_conn, monkeypatch):
        """DLV-05 全程零 AI 调用（不花钱、确定性）。"""
        ctrl = "正常内容\x07异常内容"
        await _seed(db_conn, a_content=ctrl)

        async def never(messages, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("程序化修复不得调用 AI")

        monkeypatch.setattr(review_autofix, "chat_with_fallback", never)
        f = {"rule_id": "DLV-05", "section_id": "", "title": "控制字符",
             "detail": "含控制字符", "evidence": [], "suggestion": "清除"}
        res = await self._apply_via_router(
            db_conn, finding=f, sections=_sections(a_content=ctrl))
        assert res["ok"], res
        assert res["mode"] == "auto"
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert "\x07" not in (await cur.fetchone())["content"]

    async def test_not_located_refuses_blind_rewrite(self, db_conn, monkeypatch):
        """定位不到矛盾位置 → 拒绝盲改，正文逐字不变。"""
        await _seed(db_conn)
        f = _con01_finding()
        f["evidence"] = ["999日历天（工程概况）"]   # 正文中不存在

        async def never(messages, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("定位不到时不得调用 AI 盲写整章")

        monkeypatch.setattr(review_autofix, "chat_with_fallback", never)
        res = await self._apply_via_router(db_conn, finding=f, sections=_sections())
        assert not res["ok"] and res["status"] == "not_located"
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())["content"] == _BEFORE_A

    async def test_manual_rule_never_calls_ai(self, db_conn, monkeypatch):
        await _seed(db_conn)

        async def never(messages, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("manual 规则不得调用 AI")

        monkeypatch.setattr(review_autofix, "chat_with_fallback", never)
        res = await self._apply_via_router(
            db_conn,
            finding={"rule_id": "DLV-01", "section_id": "", "title": "空章节",
                     "detail": "x", "evidence": [], "suggestion": "生成正文"},
            sections=_sections())
        assert res["status"] == "unsupported" and res["reason"]

    async def test_repair_record_mode_and_snapshot_backfilled(self, db_conn, monkeypatch):
        """留痕复用 consistency_repairs：mode=review_autofix 且快照 id 已回填。"""
        await _seed(db_conn)

        async def fake_chat(messages, **kwargs):
            return _AFTER_A

        monkeypatch.setattr(review_autofix, "chat_with_fallback", fake_chat)
        res = await self._apply_via_router(
            db_conn, finding=_con01_finding(), sections=_sections())
        cur = await db_conn.execute(
            "SELECT mode, snapshot_id, items FROM consistency_repairs WHERE id=?",
            (res["repair_id"],))
        row = await cur.fetchone()
        assert row["mode"] == "review_autofix"
        # 快照 id 必须在落库后回填，否则回滚端点拿不到凭据
        assert row["snapshot_id"] == res["snapshot_id"]
        assert isinstance(json.loads(row["items"]), list)
