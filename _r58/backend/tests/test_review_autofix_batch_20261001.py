"""审核与预检 · 自动修复批量增强（2026-10-01）

守护新增的「发现收集 → 批量暂存 → 逐条/批量确认」链路不变量：

1. **句子级定位**：``locate_targets`` 的 target 必须带 ``sentence_idx`` /
   ``sentence_total``（方案要求的「定位到句」）。
2. **暂存不落库**：``stage_fixes`` 只落一条 ``review_autofix_batch`` 记录
   （``status=pending_confirm``），**绝不**写 sections.content。
3. **同章多问题链式合并**：同一章节内多条 fixable finding 按严重度链式改写，
   合并结果正确（末条 after 即全章合并结果）。
4. **确认落库 + 联动**：``/confirm`` accept_all 写正文、重算字数、退回审核、
   失效一致性缓存，并产生可回滚快照；拒绝则正文逐字不变。
5. **prefix 守卫**：非前缀式「拒绝中间某条」由 ``_merged_after_if_prefix``
   返回 None 触发重新链式改写（防合并结果错乱）。
"""
import json

from app.services import review_autofix
from app.services.content_utils import text_word_count, word_status_for

SCHEME = "sch-batch-1"
PROJ = "prj-batch-1"
SEC_A = "sec-batch-a"
SEC_B = "sec-batch-b"

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
    return {
        "rule_id": "CON-01", "dimension": "consistency", "severity": "high",
        "title": "全文一致性", "detail": "「工期」在正文中出现 2 种不同取值",
        "evidence": ["120日历天（工程概况）", "90日历天（施工进度计划）"],
        "section_id": "", "section_title": "", "suggestion": "请统一工期口径",
        "basis": "", "mode": "program",
    }


def _dlv05_finding(section_id: str = SEC_A) -> dict:
    return {
        "rule_id": "DLV-05", "dimension": "deliverability", "severity": "block",
        "title": "控制字符", "detail": "含控制字符", "evidence": [],
        "section_id": section_id, "section_title": "工程概况",
        "suggestion": "清除控制字符", "basis": "", "mode": "program",
    }


def _dlv06_finding(section_id: str = SEC_A) -> dict:
    return {
        "rule_id": "DLV-06", "dimension": "deliverability", "severity": "medium",
        "title": "口语化", "detail": "含 AI 腔表述", "evidence": [],
        "section_id": section_id, "section_title": "工程概况",
        "suggestion": "改写为工程书面语", "basis": "", "mode": "program",
    }


def _dlv07_finding(section_id: str = SEC_A) -> dict:
    return {
        "rule_id": "DLV-07", "dimension": "deliverability", "severity": "block",
        "title": "未闭合围栏", "detail": "代码围栏未闭合", "evidence": [],
        "section_id": section_id, "section_title": "工程概况",
        "suggestion": "补齐结束围栏", "basis": "", "mode": "program",
    }


class TestSentenceLocation:
    """句子级定位：target 必须带 sentence_idx / sentence_total。"""

    def test_sentence_idx_present_for_value_anchor(self):
        f = _con01_finding()
        f["evidence"] = ["120日历天（工程概况）"]
        targets = review_autofix.locate_targets(
            f, _sections(a_content="第一行无关。\n第二行写明总工期为 120 日历天。第三行结尾。\n"))
        assert len(targets) == 1
        t = targets[0]
        assert "sentence_idx" in t and "sentence_total" in t
        # 「120 日历天」在第二段（第 2 句）
        assert t["sentence_idx"] >= 1 and t["sentence_total"] >= 2
        assert t["line"] == 2

    def test_whole_section_has_zero_sentence(self):
        targets = review_autofix.locate_targets(
            {"rule_id": "CMP-09", "section_id": SEC_A, "evidence": []}, _sections())
        assert targets and targets[0]["sentence_idx"] == 0


class TestStageFixesService:
    """stage_fixes：暂存不落库 + 同章链式合并。"""

    async def test_stage_does_not_persist_content(self, db_conn, monkeypatch):
        await _seed(db_conn, a_content="正常\x07内容")
        res = await review_autofix.stage_fixes(
            db_conn, scheme_id=SCHEME, findings=[_dlv05_finding()],
            sections=_sections(a_content="正常\x07内容"),
            scheme={"name": "S", "type": "深基坑"})
        assert res["status"] == "pending_confirm"
        assert res["batch_id"]
        assert res["stats"]["repaired"] == 1
        # 暂存阶段正文必须逐字不变（落库由 /confirm 完成）
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert "\x07" in (await cur.fetchone())["content"]

    async def test_stage_record_mode_and_status(self, db_conn, monkeypatch):
        await _seed(db_conn, a_content="正常\x07内容")
        res = await review_autofix.stage_fixes(
            db_conn, scheme_id=SCHEME, findings=[_dlv05_finding()],
            sections=_sections(a_content="正常\x07内容"),
            scheme={"name": "S", "type": "深基坑"})
        cur = await db_conn.execute(
            "SELECT mode, status FROM consistency_repairs WHERE id=?",
            (res["batch_id"],))
        row = await cur.fetchone()
        assert row["mode"] == "review_autofix_batch"
        assert row["status"] == "pending_confirm"

    async def test_chain_same_section_merges_two_fixes(self, db_conn, monkeypatch):
        """同章 DLV-05（控制字符）+ DLV-07（未闭合围栏）链式合并，两条都修掉。"""
        await _seed(db_conn, a_content="正常\x07内容\n```mermaid\ngraph TD; A-->B\n")
        res = await review_autofix.stage_fixes(
            db_conn, scheme_id=SCHEME,
            findings=[_dlv05_finding(), _dlv07_finding()],
            sections=_sections(a_content="正常\x07内容\n```mermaid\ngraph TD; A-->B\n"),
            scheme={"name": "S", "type": "深基坑"})
        items = {it["rule_id"]: it for it in res["items"]}
        assert items["DLV-05"]["status"] == "repaired"
        assert items["DLV-07"]["status"] == "repaired"
        # 两条 finding 带链式顺序
        assert items["DLV-05"]["chain_index"] != items["DLV-07"]["chain_index"]
        # 末条 after 应同时不含控制字符且围栏已闭合
        last = max(res["items"], key=lambda x: x["chain_index"])
        assert "\x07" not in last["after"]
        assert last["after"].count("```") % 2 == 0


class TestConfirmFlowRouter:
    """/confirm：落库 + 联动 / 拒绝不落库。"""

    async def _stage_and_confirm(self, db_conn, monkeypatch, *, body,
                                 after_seed=None):
        """暂存 → 确认。``after_seed`` 用于在**建数据之后、stage 之前**做额外布置。

        ⚠️ 必须提供该钩子而不是在调用方预先 UPDATE：``_seed`` 内部才 INSERT
        sections，调用方的 UPDATE 会打在空表上（影响 0 行、**不报错**），
        于是「章节处于 approved」这一前提根本没成立，断言必然失败且极难定位。
        """
        from app.routers import review_autofix as ra

        async def fake_overview(db, scheme_id, **_kw):
            # _kw 容纳 persist=False（2026-10-03 数据链收口：修复链路重算
            # 不得落 preflight_runs）；本锁兼防旧式两参调用回退。
            return {"findings": [_dlv05_finding()], "content_fingerprint": "fp",
                    "stale": False}

        monkeypatch.setattr(
            "app.routers.compliance._readiness_overview_compute", fake_overview)
        await _seed(db_conn, a_content="正常\x07内容")
        if after_seed is not None:
            await after_seed(db_conn)
            await db_conn.commit()
        stage_res = await ra.stage(SCHEME, {"rule_ids": ["DLV-05"]}, db_conn)
        assert stage_res["batch_id"], stage_res
        return await ra.confirm(SCHEME, {**body, "batch_id": stage_res["batch_id"]},
                                 db_conn)

    async def test_accept_all_persists_and_resets_review(self, db_conn, monkeypatch):
        async def _mark_approved(conn):
            await conn.execute(
                "UPDATE sections SET review_status='approved' WHERE id=?", (SEC_A,))

        res = await self._stage_and_confirm(
            db_conn, monkeypatch, body={"accept_all": True},
            after_seed=_mark_approved)
        assert res["status"] == "confirmed"
        assert res["snapshot_id"]
        cur = await db_conn.execute(
            "SELECT content, review_status FROM sections WHERE id=?", (SEC_A,))
        row = await cur.fetchone()
        assert "\x07" not in row["content"]
        assert row["review_status"] == "pending"
        # 批次状态置为 confirmed
        cur = await db_conn.execute(
            "SELECT status FROM consistency_repairs WHERE id=?", (res["batch_id"],))
        assert (await cur.fetchone())["status"] == "confirmed"

    async def test_reject_keeps_original(self, db_conn, monkeypatch):
        res = await self._stage_and_confirm(
            db_conn, monkeypatch, body={"reject": ["DLV-05"]})
        assert res["status"] == "rejected"
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert "\x07" in (await cur.fetchone())["content"]

    async def test_partial_accept_prefix_takes_merged(self, db_conn, monkeypatch):
        """同章两条可修问题，accept 子集恰为前缀 → 直接取末条 after（不重调 AI）。"""
        from app.routers import review_autofix as ra

        async def fake_overview(db, scheme_id, **_kw):
            return {"findings": [_dlv05_finding(), _dlv07_finding()],
                    "content_fingerprint": "fp", "stale": False}

        monkeypatch.setattr(
            "app.routers.compliance._readiness_overview_compute", fake_overview)
        # 两条都 auto，不需要 AI
        await _seed(db_conn, a_content="正常\x07内容\n```mermaid\ngraph TD; A-->B\n")
        stage_res = await ra.stage(
            SCHEME, {"rule_ids": ["DLV-05", "DLV-07"]}, db_conn)
        # 接受 DLV-05（链式前缀第一条）→ 合并后即 DLV-05 之后的状态（围栏仍未闭）
        confirm_res = await ra.confirm(
            SCHEME, {"accept": ["DLV-05"], "batch_id": stage_res["batch_id"]},
            db_conn)
        assert confirm_res["status"] == "confirmed"
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC_A,))
        content = (await cur.fetchone())["content"]
        assert "\x07" not in content  # DLV-05 已修
        # 仅接受前缀第一条，DLV-07（围栏）未被接受 → 围栏仍为未闭合（奇数个 ```）
        assert content.count("```") % 2 == 1


class TestPrefixGuard:
    """_merged_after_if_prefix：非前缀拒绝必须返回 None（触发重算）。"""

    def test_prefix_accept_returns_merged(self):
        items = [
            {"rule_id": "A", "chain_index": 0, "after": "after-A"},
            {"rule_id": "B", "chain_index": 1, "after": "after-B"},
        ]
        assert review_autofix  # 占位，确保导入
        from app.routers import review_autofix as ra
        assert ra._merged_after_if_prefix(items, {"A", "B"}) == "after-B"
        assert ra._merged_after_if_prefix(items, {"A"}) == "after-A"

    def test_non_prefix_reject_returns_none(self):
        items = [
            {"rule_id": "A", "chain_index": 0, "after": "after-A"},
            {"rule_id": "B", "chain_index": 1, "after": "after-B"},
        ]
        from app.routers import review_autofix as ra
        # 拒绝中间（接受 B 但拒绝 A）→ 非前缀
        assert ra._merged_after_if_prefix(items, {"B"}) is None
