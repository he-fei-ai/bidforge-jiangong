"""R122（2026-10-21 · 易标六机制引入）护栏测试。

覆盖三项引入缺口（G1 编排选中事实强注入 / G2 整卷合并一致性修复 /
G3 成稿后跨章节字数批量调度），按方案步骤 6 的四类断言：

A 组 —— 开关默认关 + 关闭态逐字节等价（行为级 + 源码锁）；
B 组 —— G1 白名单钳制与豁免/前置（纯函数行为）；
C 组 —— G2 工作区装配/解析与四道闸门（纯函数 + run_fullbook_repair 行为级，
        chat_with_fallback 打桩、零真实 AI）；
D 组 —— G3 分批顺序与判据同源（纯函数 + 与既有执行链判据 parity）。

铁律证据面：所有「关闭态等价」断言只依赖默认配置；开启态行为一律打桩，
绝不打真实 provider。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import settings
from app.services import consistency_fullbook_repair as cfr
from app.services import repair_record
from app.services.ai.prompts._registry import PROMPT_VARIABLE_CONTRACTS
from app.services.consistency_fullbook_repair import (
    FULLBOOK_MAX_CHARS,
    FULLBOOK_MAX_RETRIES,
    _append_repair_nudge,
    _digest_conflicts,
    _section_conflict_values,
    build_fullbook_workspace,
    parse_fullbook_workspace,
)
from app.services.content_plan import resolve_selected_fact_rows
from app.services.content_runtime import should_continue_round
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    LEAF_MIN_WORD_BUDGET,
    WORD_UNDER_RATIO,
)
from app.services.facts_builder import (
    _render_facts_text,
    fact_injection_audit,
)
from app.services.word_adjust_batch import (
    DIRECTION_OK,
    DIRECTION_OVER,
    DIRECTION_UNDER,
    WORD_ADJUST_BATCH_SIZE,
    WORD_ADJUST_CONCURRENCY,
    WORD_ADJUST_MAX_ROUNDS_PER_SECTION,
    build_adjust_plan,
    classify_word_direction,
    continue_trigger_line,
)

_SSE_SRC = (Path(__file__).parents[1] / "app/routers/sse_handlers.py").read_text(encoding="utf-8")
_ROUTE_SRC = (Path(__file__).parents[1] / "app/routers/consistency_repair.py").read_text(encoding="utf-8")


def _row(title: str, content: str, chapter: str = "", kind: str = "value"):
    """facts_rows 6 元组契约：(group_title, title, content, confidence, chapter, value_kind)"""
    return ("施工参数", title, content, 0.95, chapter, kind)


# ============ A 组：开关默认关 + 关闭态等价 ============


class TestSwitchDefaultsAndEquivalence:
    def test_three_switches_default_false(self):
        """新增 3 配置项默认必须全关（引入铁律：默认关闭）。"""
        assert settings.content_plan_selected_facts_inject is False
        assert settings.consistency_fullbook_repair_enabled is False
        assert settings.word_adjust_batch_enabled is False

    def test_render_facts_closed_state_byte_equal(self):
        """full_titles 缺省/空/全不命中三形态输出逐字节一致（关闭态零变化）。"""
        rows = [
            _row("混凝土强度等级", "C35", chapter="施工工艺技术"),
            _row("工期", "日历 120 天", chapter="施工计划"),
            _row("项目经理", "张工（一级建造师）"),
        ]
        base = _render_facts_text(rows)
        assert _render_facts_text(rows, full_titles=None) == base
        assert _render_facts_text(rows, full_titles=[]) == base
        assert _render_facts_text(rows, full_titles=["不存在的标题"]) == base
        # 命中任一标题则必须偏离（检测力：等价断言不是恒真）
        assert _render_facts_text(rows, full_titles=["工期"]) != base

    def test_audit_closed_state_matches_plain_render(self):
        """fact_injection_audit 缺省形态 rendered_text 与裸渲染逐字节一致、
        selected_titles 空列表（R105 既有契约不破坏 + R122 键只增不改）。"""
        rows = [_row("周转材料", "钢管脚手架若干")]
        plain = _render_facts_text(rows)
        audit = fact_injection_audit(rows)
        assert audit["rendered_text"] == plain
        assert audit["selected_titles"] == []

    def test_sse_g1_wiring_is_gated(self):
        """G1 接线源码锁：选中事实重算必须在 content_plan_selected_facts_inject
        门控内（以代码形态定位，非注释），且走 resolve_selected_fact_rows +
        full_titles 透传。"""
        anchor = '"content_plan_selected_facts_inject", False'
        assert anchor in _SSE_SRC
        seg = _SSE_SRC[_SSE_SRC.index(anchor) :]
        seg = seg[: seg.index("_chapter_facts_text = _chapter_audit")]
        assert "resolve_selected_fact_rows" in seg
        assert "full_titles=_plan.fact_titles" in seg

    def test_sse_g2_branch_keeps_legacy_default(self):
        """G2 分流源码锁：门控 else 分支仍调 _repair_agent.run_repair（关闭态
        逐字节走既有逐章链路）。"""
        assert "consistency_fullbook_repair_enabled" in _SSE_SRC
        anchor = '"consistency_fullbook_repair_enabled", False'
        assert anchor in _SSE_SRC
        seg = _SSE_SRC[_SSE_SRC.index(anchor) : _SSE_SRC.index(anchor) + 1600]
        assert "run_fullbook_repair as _run_fullbook" in seg
        assert "_repair_agent.run_repair" in seg

    def test_sse_g3_block_is_gated_and_planned(self):
        """G3 接线源码锁：批调度整段在 word_adjust_batch_enabled 门控后（代码
        形态定位），计划走 build_adjust_plan、并发走 WORD_ADJUST_CONCURRENCY、
        事件名齐备。"""
        anchor = '"word_adjust_batch_enabled", False'
        assert anchor in _SSE_SRC
        # ✅ R194 重锚：FIX-5（over 分支补上下文 / 计数抑制 / 停止出口）使批
        #    调度块合法增长 ~2.7k 字符，窗口 12000 → 16000（意图不变：整段块
        #    含四个事件名，窗口必须盖住块尾）。
        seg = _SSE_SRC[_SSE_SRC.index(anchor) : _SSE_SRC.index(anchor) + 16000]
        assert "build_adjust_plan(_adj_entries)" in seg
        assert "WORD_ADJUST_CONCURRENCY" in seg
        for ev in ("word_adjust_start", "word_adjust_batch_start", "word_adjust_batch_done", "word_adjust_done"):
            assert ev in seg

    async def test_route_fullbook_422(self, db_conn):
        from app.routers import consistency_repair as route
        from fastapi import HTTPException

        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name, type) " "VALUES ('sch-a2', 'p1', '测试方案', '吊装')"
        )
        await db_conn.execute(
            "INSERT INTO consistency_conflicts (id, scheme_id, scan_id, "
            "conflict_type, severity, topic, occurrences, "
            "authoritative_value, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "c1",
                "sch-a2",
                "scan-1",
                "value",
                "high",
                "强度等级",
                '[{"section_id":"s1","value":"C30"}]',
                "C35",
                "2026-10-21",
            ),
        )
        await db_conn.commit()
        with pytest.raises(HTTPException) as ei:
            await route.repair("sch-a2", body={"scan_id": "scan-1", "mode": "fullbook"}, db=db_conn)
        assert ei.value.status_code == 422
        assert "未启用" in str(ei.value.detail)


# ============ B 组：G1 白名单钳制与豁免/前置 ============


class TestG1SelectedFacts:
    def test_whitelist_clamp_only_selected_rows_returned(self):
        rows = [_row("工期", "120 天"), _row("质保期", "24 个月"), _row("混凝土等级", "C35")]
        got = resolve_selected_fact_rows(["工期"], rows)
        assert got == [rows[0]]  # 未选中的行绝不泄进白名单出口

    def test_titles_absent_in_rows_are_dropped(self):
        rows = [_row("工期", "120 天")]
        assert resolve_selected_fact_rows(["编造的事实"], rows) == []
        assert resolve_selected_fact_rows(None, rows) == []
        assert resolve_selected_fact_rows(["工期"], None) == []

    def test_dirty_rows_tolerated(self):
        rows = [7, ("短行",), _row("人员", "8 名普工")]
        got = resolve_selected_fact_rows(["人员", ""], rows)
        assert got == [rows[2]]

    def test_selected_row_exempt_from_per_fact_truncation(self):
        long_val = "要求" * 400  # 800 字 > per_fact=300
        rows = [_row("抗震措施", long_val), _row("工期", "120 天")]
        plain = _render_facts_text(rows, per_fact=300)
        strong = _render_facts_text(rows, per_fact=300, full_titles=["抗震措施"])
        assert long_val not in plain  # 未选中：截断到 300
        assert long_val in strong  # 选中：全长豁免

    def test_selected_rows_prepended(self):
        # 渲染形态为「组头 + 内容」（不含事实标题）—— 用内容串位置断言前置
        rows = [_row("甲事实", "A" * 50), _row("乙事实", "B" * 50)]
        out = _render_facts_text(rows, full_titles=["乙事实"])
        assert out.index("B" * 50) < out.index("A" * 50)
        # 反锚：未强注入时保持原序（A 在前）
        base = _render_facts_text(rows)
        assert base.index("A" * 50) < base.index("B" * 50)

    def test_audit_selected_titles_echo(self):
        rows = [_row("工期", "120 天"), _row("质保", "24 个月")]
        audit = fact_injection_audit(rows, full_titles=["质保", "不存在"])
        assert audit["selected_titles"] == ["质保"]  # 只回显真命中

    def test_contract_registry_has_fullbook_prompts(self):
        assert "consistency_fullbook_system" in PROMPT_VARIABLE_CONTRACTS
        assert PROMPT_VARIABLE_CONTRACTS["consistency_fullbook_system"] == []
        assert PROMPT_VARIABLE_CONTRACTS["consistency_fullbook_user"] == ["section_count", "workspace"]

    def test_fullbook_prompts_render(self):
        from app.services.ai.prompts._registry import render

        sys_text = render("consistency_fullbook_system")
        assert "<!--" in sys_text  # 标记纪律写进 system
        user_text = render("consistency_fullbook_user", section_count="3", workspace="WS 正文")
        assert "3" in user_text and "WS 正文" in user_text


# ============ C 组：G2 工作区与四道闸门 ============


class TestG2WorkspaceAndGates:
    def test_build_parse_roundtrip(self):
        sections = [
            {"id": "s1", "title": "第一章", "content": "内容一"},
            {"id": "s2", "title": "第二章", "content": "内容二"},
        ]
        ws = build_fullbook_workspace(facts="F", conflicts_digest="D", sections=sections)
        parsed, unknown = parse_fullbook_workspace(ws, ["s1", "s2"])
        assert unknown == []
        assert parsed["s1"].strip() == "内容一"
        assert parsed["s2"].strip() == "内容二"

    def test_unknown_id_collected_not_merged(self):
        ws = "<!-- §:ghost -->\n幽灵章节正文\n"
        parsed, unknown = parse_fullbook_workspace(ws, ["s1"])
        assert parsed == {}
        assert unknown == ["ghost"]

    def test_duplicate_id_flagged(self):
        ws = "<!-- §:s1 -->\n正文A\n<!-- §:s1 -->\n正文B\n"
        parsed, unknown = parse_fullbook_workspace(ws, ["s1"])
        assert parsed["s1"].strip() == "正文A"  # 首块为准
        assert unknown == ["s1(duplicate)"]  # 重复触发整包拒

    def test_marker_must_own_line(self):
        # 行中小节文字里的标记**不是**分隔符（独占一行才成立）
        text = "前文 <!-- §:s1 --> 后文\n正文"
        parsed, unknown = parse_fullbook_workspace(text, ["s1"])
        assert parsed == {} and unknown == []

    def test_digest_skips_unfixable(self):
        conflicts = [
            {"id": "c1", "topic": "T1", "authoritative_value": "", "occurrences": []},  # 无权威值 → 不进清单
            {
                "id": "c2",
                "conflict_type": "value",
                "topic": "强度",
                "authoritative_value": "C35",
                "occurrences": [{"value": "C30"}, {"value": "C35"}],
                "repair_instruction": "统一为 C35",
            },
        ]
        d = _digest_conflicts(conflicts)
        assert "c1" not in d
        assert "C35" in d and "C30" in d and "统一为 C35" in d

    def test_section_conflict_values_unique_authority(self):
        conflicts = [
            {
                "id": "c1",
                "authoritative_value": "C35",
                "occurrences": [{"section_id": "s1", "value": "C30"}, {"section_id": "s2", "value": "C25"}],
            }
        ]
        wrong, auth = _section_conflict_values(conflicts, "s1")
        assert wrong == ["C30"] and auth == "C35"
        # 多权威值 → auth 空（validate 不检查权威出现，只查残留）
        conflicts.append(
            {"id": "c2", "authoritative_value": "24个月", "occurrences": [{"section_id": "s1", "value": "12个月"}]}
        )
        wrong2, auth2 = _section_conflict_values(conflicts, "s1")
        assert wrong2 == ["12个月", "C30"] and auth2 == ""

    def test_nudge_appends_directed_repair_message(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        out = _append_repair_nudge(msgs, "回包含未知小节")
        assert len(out) == 3 and out[-1]["role"] == "user"
        assert "回包含未知小节" in out[-1]["content"]
        assert "重新输出完整整卷" in out[-1]["content"]

    def test_constants_lock(self):
        """FULLBOOK_MAX_RETRIES=2 对齐易标 maxRetries=2（1 生成 + 2 重问 = 3 次）；
        工作区上限 20 万字符防线不得被随手调小/调大而无视。"""
        assert FULLBOOK_MAX_RETRIES == 2
        assert FULLBOOK_MAX_CHARS == 200000

    async def test_run_fullbook_empty_targets_no_ai(self, db_conn, monkeypatch):
        """无可修冲突：零 AI 调用，快照 + 空批次落库（save_repair 链路通）。"""
        calls = []

        async def _boom(*a, **k):
            calls.append(1)
            raise AssertionError("不应调用 AI")

        monkeypatch.setattr(cfr, "chat_with_fallback", _boom)
        result = await cfr.run_fullbook_repair(db_conn, scheme_id="sch-x", scan_id="scan-x", conflicts=[], facts="F")
        assert not calls
        assert result["repaired"] == 0 and result["failed"] == 0
        assert result["items"] == []

    async def _seed_two_sections(self, db_conn):
        for sid, content in (("s1", "本章采用 C30 混凝土浇筑施工。"), ("s2", "垫层使用 C30 混凝土浇筑完成。")):
            await db_conn.execute(
                "INSERT INTO sections (id, scheme_id, title, content, level,"
                " sort_order) VALUES (?, 'sch-g', ?, ?, 2, ?)",
                (sid, f"第{sid}节", content, sid),
            )
        await db_conn.commit()

    def _conflict(self):
        return {
            "id": "c1",
            "conflict_type": "value",
            "severity": "high",
            "topic": "混凝土强度",
            "authoritative_value": "C35",
            "repair_instruction": "",
            "status": "pending",
            "occurrences": [{"section_id": "s1", "value": "C30"}, {"section_id": "s2", "value": "C30"}],
        }

    async def test_gate_a_unknown_id_rejects_whole_batch(self, db_conn, monkeypatch):
        await self._seed_two_sections(db_conn)
        ws_sections = [
            {"id": "s1", "title": "第s1节", "content": "本章采用 C35 混凝土浇筑施工。"},
            {"id": "s2", "title": "第s2节", "content": "垫层使用 C35 混凝土浇筑完成。"},
            {"id": "ghost", "title": "幽灵", "content": "X"},
        ]
        reply = build_fullbook_workspace(facts="F", conflicts_digest="D", sections=ws_sections)

        async def _fake(messages, **kw):
            return reply

        monkeypatch.setattr(cfr, "chat_with_fallback", _fake)
        result = await cfr.run_fullbook_repair(
            db_conn, scheme_id="sch-g", scan_id="scan-g", conflicts=[self._conflict()], facts="F"
        )
        assert result["repaired"] == 0
        # 正文逐字未动（闸门 A 整包拒 ⇒ 零回写）
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='s1'")
        row = await cur.fetchone()
        assert "C30" in row["content"]

    async def test_gate_b_missing_section_rejects_whole_batch(self, db_conn, monkeypatch):
        await self._seed_two_sections(db_conn)
        # 回包只带 s1（丢 s2）
        reply = "<!-- §:s1 -->\n# 第s1节\n本章采用 C35 混凝土浇筑施工。\n"

        async def _fake(messages, **kw):
            return reply

        monkeypatch.setattr(cfr, "chat_with_fallback", _fake)
        result = await cfr.run_fullbook_repair(
            db_conn, scheme_id="sch-g", scan_id="scan-g", conflicts=[self._conflict()], facts="F"
        )
        assert result["repaired"] == 0
        assert "缺" in result["items"][0]["problems"][0]

    async def test_gate_c_nonempty_to_empty_rejects_that_section(self, db_conn, monkeypatch):
        await self._seed_two_sections(db_conn)
        ws_sections = [
            {"id": "s1", "title": "第s1节", "content": "本章采用 C35 混凝土浇筑施工。"},
            {"id": "s2", "title": "第s2节", "content": ""},
        ]
        reply = build_fullbook_workspace(facts="F", conflicts_digest="D", sections=ws_sections)

        async def _fake(messages, **kw):
            return reply

        monkeypatch.setattr(cfr, "chat_with_fallback", _fake)
        result = await cfr.run_fullbook_repair(
            db_conn, scheme_id="sch-g", scan_id="scan-g", conflicts=[self._conflict()], facts="F"
        )
        by_section = {i.get("section_id"): i["status"] for i in result["items"]}
        assert by_section["s1"] == "repaired"  # 合规改法回写
        assert by_section["s2"] == "failed"  # 非空改空被闸
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='s2'")
        row = await cur.fetchone()
        assert row["content"] == "垫层使用 C30 混凝土浇筑完成。"  # 原文保留
        cur1 = await db_conn.execute("SELECT content FROM sections WHERE id='s1'")
        assert "C35" in (await cur1.fetchone())["content"]


# ============ D 组：G3 判据同源与分批 ============


class TestG3BatchPlanner:
    def test_constants_lock(self):
        assert WORD_ADJUST_BATCH_SIZE == 10  # 易标 TOTAL_WORD_ADJUSTMENT_BATCH_SIZE
        assert WORD_ADJUST_MAX_ROUNDS_PER_SECTION == 3  # 易标 MAX_WORD_ADJUSTMENT_ROUNDS
        assert WORD_ADJUST_CONCURRENCY == 3

    @pytest.mark.parametrize(
        "wc,wb",
        [
            (100, 1200),
            (900, 1200),
            (950, 1200),
            (1000, 1200),
            (80, 100),
            (100, 100),
            (120, 100),
            (1500, 1000),
            (0, 300),
            (239, 300),
        ],
    )
    def test_under_class_parity_with_continue_round(self, wc, wb):
        """classify「under」⇔ 既有续写链首轮会触发（执行器不空转、计划不谎报）。"""
        trig = should_continue_round(
            wc=wc,
            word_budget=wb,
            continue_count=0,
            min_passes=0,
            max_rounds=3,
            min_floor_chars=min(LEAF_MIN_WORD_BUDGET, int(wb or 0)),
        )
        assert (classify_word_direction(wc, wb) == DIRECTION_UNDER) == trig

    def test_trigger_line_formula(self):
        assert continue_trigger_line(1200) == 1200 * WORD_UNDER_RATIO
        assert continue_trigger_line(100) == 100  # min(300,100)=100 取全额
        assert continue_trigger_line(0) == DEFAULT_WORD_BUDGET * WORD_UNDER_RATIO

    def test_over_class_parity_with_auto_shrink(self):
        """「over」⇔ _should_auto_shrink 同判据（计划说缩 → 执行真会缩）。"""
        from app.routers.sse_handlers import _should_auto_shrink

        for wc, wb in ((1500, 1000), (1299, 1000), (1301, 1000), (900, 1200)):
            content = "文" * wc
            assert (classify_word_direction(wc, wb) == DIRECTION_OVER) == _should_auto_shrink(content, wb)

    def test_batch_split_order_and_counts(self):
        entries = [{"section_id": f"s{i}", "title": f"T{i}", "word_count": 100, "word_budget": 1200} for i in range(25)]
        plan = build_adjust_plan(entries)
        assert [len(b) for b in plan["batches"]] == [10, 10, 5]
        assert plan["under_count"] == 25 and plan["ok_count"] == 0
        # 批序即目录序（输入序），flatten == items，绝无重排
        flat = [it["section_id"] for b in plan["batches"] for it in b]
        assert flat == [it["section_id"] for it in plan["items"]]
        assert flat == [f"s{i}" for i in range(25)]

    def test_ok_excluded_mixed_order_preserved(self):
        entries = [
            {"section_id": "a", "title": "A", "word_count": 100, "word_budget": 1200},  # under
            {"section_id": "b", "title": "B", "word_count": 1000, "word_budget": 1200},  # ok
            {"section_id": "c", "title": "C", "word_count": 1600, "word_budget": 1000},  # over
        ]
        plan = build_adjust_plan(entries)
        assert [i["section_id"] for i in plan["items"]] == ["a", "c"]
        assert plan["items"][0]["direction"] == DIRECTION_UNDER
        assert plan["items"][1]["direction"] == DIRECTION_OVER
        assert plan["ok_count"] == 1 and plan["total_sections"] == 3

    def test_duplicate_section_id_keeps_first(self):
        entries = [
            {"section_id": "x", "title": "先", "word_count": 10, "word_budget": 1200},
            {"section_id": "x", "title": "后", "word_count": 20, "word_budget": 1200},
        ]
        plan = build_adjust_plan(entries)
        assert len(plan["items"]) == 1
        assert plan["items"][0]["title"] == "先"
        assert plan["total_sections"] == 2  # total 记输入行数（重复仅 items 去重）

    def test_dirty_inputs_never_raise(self):
        plan = build_adjust_plan([None, 7, {}, {"section_id": "s"}])
        assert plan["batches"]  # 缺字段按 0 字 → under，不抛
        assert build_adjust_plan([])["items"] == []

    def test_batch_size_clamped(self):
        entries = [{"section_id": f"s{i}", "word_count": 100, "word_budget": 1200} for i in range(5)]
        assert len(build_adjust_plan(entries, batch_size=2)["batches"]) == 3
        assert len(build_adjust_plan(entries, batch_size=0)["batches"]) == 5
        assert len(build_adjust_plan(entries, batch_size=-3)["batches"]) == 5

    async def test_save_repair_mode_fullbook_roundtrip(self, db_conn):
        """save_repair 既有链路接受 mode="fullbook"（confirm/rollback 共用
        不新造表不新造状态机）。"""
        snap_id = await repair_record.create_snapshot(db_conn, "sch-r", [])
        result = await repair_record.save_repair(
            db_conn,
            scheme_id="sch-r",
            scan_id="scan-r",
            mode="fullbook",
            items=[],
            snapshot_id=snap_id,
            total_conflicts=0,
            stats={"repaired": 0, "failed": 0, "skipped": 0},
        )
        assert result["repair_id"]
        assert json.dumps(result, ensure_ascii=False)  # 结果可序列化（SSE 载荷）
