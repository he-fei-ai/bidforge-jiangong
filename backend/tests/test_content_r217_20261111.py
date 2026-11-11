"""R217 · 正文生成模块第三轮深度审计护栏（2026-11-11）。

覆盖本轮落地修复（详见 docs/正文生成模块深度审计增强_R217_20261111.md）：

- FIX-0  R215 C4「min_words 净化后计字」修复被覆盖，只剩注释与死 import（基线 2 红）
- FIX-1  R216 SSE-7 指纹 part11 引用未定义名 `_facts_char_rows`（F821 / 运行期 NameError）
- FIX-2  guarded_gen 未预期异常补发 `section_error` 是裸 JSON 帧 ⇒ 前端整块丢弃
- FIX-3  R215 D7 把装配降级事件 put 进**已停用**的 event_queue（丢失 + 可挂死收尾）
- FIX-4  user_content 同步点夹在两次上下文治理之间 ⇒ 首轮与续写上下文分叉
- FIX-5  事实注入门控四档 vs 底稿「事实待确认」清单三档（冲突/模拟事实双重消失）
- FIX-6  R208 三个「统计失败」留痕键未登记断线重挂白名单
- FIX-7  content_plan 的 table_needed 布尔归一仍两入口内联（同族漏改防回流）
- FIX-8  R90 源码锁重锚：事务控制语句放行、业务裸写禁令强度不减（护栏在既有文件）

纪律：每条修复同时给「行为级/契约级」与「反向棘轮（旧形态不得回流）」；
源码级断言一律以**当前工作树**为准，不采信报告文字。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.checkpoint_schema import CHECKPOINT_KINDS
from app.services.content_plan import ContentPlanData, coerce_bool_flag
from app.services.content_sanitize import normalize_raw_content
from app.services.content_utils import text_word_count

_REPO = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


_SH = _src("backend/app/routers/sse_handlers.py")
_CP = _src("backend/app/routers/_chart_pipeline.py")
_PLAN = _src("backend/app/services/content_plan.py")
_DOSSIER = _src("backend/app/services/dossier.py")
_PAYLOADS = _src("backend/app/routers/_sse_content_payloads.py")
_FACTS = _src("backend/app/services/facts_extractor.py")
_SCHEMA_TS = _src("frontend/src/types/checkpointSchema.ts")
_API_TS = _src("frontend/src/api/index.ts")


def _window(src: str, anchor: str, before: int = 0, after: int = 700) -> str:
    i = src.find(anchor)
    assert i >= 0, f"源码锚点不存在：{anchor}"
    return src[max(0, i - before) : i + after]


# ---------------------------------------------------------------------------
# FIX-0 · min_words 判据（R215 C4 回归）
# ---------------------------------------------------------------------------
class TestMinWordsJudgeRestored:
    def test_judge_uses_sanitized_word_count(self):
        assert "_actual_words = text_word_count(normalize_raw_content(content)[0])" in _CP
        assert "from app.services.content_sanitize import normalize_raw_content" in _CP
        # 反向棘轮：净化前计字（R215 回归态）不得回流
        assert "_actual_words = text_word_count(content)" not in _CP

    def test_import_is_actually_consumed_not_dead_code(self):
        # 回归态铁证：import 在但判据里零调用 ⇒ 只有注释落地
        assert "text_word_count(normalize_raw_content(content)[0])" in _CP
        assert _CP.count("normalize_raw_content(") >= 1

    def test_footer_noise_really_inflates_pre_sanitize_count(self):
        """行为级：证明「净化前/后」两个基数确实不同（否则该判据无意义）。"""
        body = "本工程基坑支护采用钻孔灌注桩并按分区分段推进施工。" * 4
        noisy = body + "\n第 1 页 共 3 页\n" * 20
        clean, stats = normalize_raw_content(noisy)
        assert stats["page_marks"] >= 20
        assert text_word_count(noisy) > text_word_count(clean)
        assert normalize_raw_content(clean)[0] == clean  # 幂等：仅改判据不改落库正文


# ---------------------------------------------------------------------------
# FIX-1 · F821 未定义名（R216 遗留）
# ---------------------------------------------------------------------------
class TestFactsFingerprintNameDefined:
    def test_fingerprint_loads_facts_rows_directly(self):
        assert "_fp_facts_rows = await _load_facts_rows(db, scheme_id)" in _SH
        assert "_facts_char_rows if _facts_char_rows is not None" not in _SH

    def test_undefined_name_only_lives_in_its_own_closure(self):
        # `_facts_char_rows` 只允许出现在带 nonlocal 声明的那个闭包内；
        # 旧缺陷形态（在另一作用域做 `X if X is not None`）不得回流。
        blk = _window(_SH, "nonlocal _facts_char_rows", before=120, after=420)
        assert "_facts_char_rows if _facts_char_rows is not None" not in blk
        assert _SH.count("nonlocal _facts_char_rows") == 1


# ---------------------------------------------------------------------------
# FIX-2 · section_error 必须是 data: 帧
# ---------------------------------------------------------------------------
class TestSectionErrorFramePrefix:
    def test_exception_frame_is_prefixed(self):
        # 锚定 guarded_gen 异常补发分支自身（"event": "section_error" 在文件里
        # 有多处，find 的第一处不是本分支）
        blk = _window(_SH, "本分支绕过 :6209", before=120, after=700)
        assert '"data: "' in blk
        assert "json.dumps(" in blk
        assert "await event_queue.put(" in blk

    def test_bare_frame_form_not_returned(self):
        # 旧形态：put( 后直接 json.dumps(，无 "data: " 拼接
        assert "await event_queue.put(\n                                json.dumps(" not in _SH.replace("\r\n", "\n")

    def test_frontend_parser_drops_frames_without_data_line(self):
        blk = _window(_API_TS, "export function parseSseEventBlock", after=800)
        assert 'rawLine.startsWith("data:")' in blk
        assert "data: data.length ? data.join" in blk
        # 无 data: 行时返回 undefined ⇒ 调用方整块跳过
        assert ': undefined' in blk

    def test_prefixed_frame_is_parseable(self):
        """契约级：补前缀后的帧可被「取 data 行 → json.loads」链路消费。"""
        frame = "data: " + json.dumps({"event": "section_error", "reason": "x"}, ensure_ascii=False) + "\n\n"
        data_lines = [ln[5:].lstrip() for ln in frame.splitlines() if ln.startswith("data:")]
        assert data_lines and json.loads("\n".join(data_lines))["event"] == "section_error"


# ---------------------------------------------------------------------------
# FIX-3 · 批调度降级事件不得进已停用队列
# ---------------------------------------------------------------------------
class TestDegradedEventsAfterTerminal:
    def test_no_put_into_retired_event_queue(self):
        assert "await event_queue.put(_cd_event)" not in _SH

    def test_unique_queue_consumer_breaks_on_sentinel(self):
        blk = _window(_SH, "line = await event_queue.get()", before=80, after=240)
        assert "while True:" in blk and "DONE_SENTINEL" in blk
        assert "break" in blk and "yield line" in blk
        assert _SH.count("line = await event_queue.get()") == 1

    def test_flush_happens_in_main_coroutine_after_gather(self):
        anchor = "await asyncio.gather(*_adj_tasks)"
        blk = _window(_SH, anchor, after=620)
        assert "for _cd_event in _flush_context_degraded():" in blk
        assert "yield _cd_event" in blk
        assert blk.index("yield _cd_event") > blk.index(anchor)

    def test_flush_not_bypassed_by_early_returns(self):
        # 早退 return 仍在 _adjust_one 内，但 flush 只在批末主协程 ⇒ 无绕过窗口
        early = _window(_SH, "if _cont_failed or _new == _content:", after=520)
        assert "_flush_context_degraded" not in early.split("if _new == _content:")[0]
        assert _SH.count("for _cd_event in _flush_context_degraded():") >= 3  # gen_one×2 + 批末

    def test_degraded_event_format_is_a_complete_sse_frame(self):
        # 主协程直接 yield，故事件串本身必须是完整帧（单一出口 _context_degraded_events）
        blk = _window(_SH, 'events.append("data: "', after=180)
        assert 'json.dumps(payload, ensure_ascii=False) + "\\n\\n"' in blk


# ---------------------------------------------------------------------------
# FIX-4 · user_content 同步点在两次治理之后
# ---------------------------------------------------------------------------
class TestUserContentSyncOrder:
    def test_sync_after_both_governors(self):
        i_budget = _SH.index("_apply_prompt_context_budget_to_messages(messages)")
        i_limit = _SH.index("_apply_context_length_limit_to_messages(messages)")
        i_sync = _SH.index('user_content = "".join(')
        assert i_budget < i_limit < i_sync, "同步点必须夹在两次治理之后"

    def test_old_order_not_returned(self):
        blk = _window(_SH, "_apply_prompt_context_budget_to_messages(messages)", after=900)
        i_limit = blk.find("_apply_context_length_limit_to_messages(messages)")
        i_sync = blk.find('user_content = "".join(')
        assert 0 <= i_limit < i_sync, "旧顺序（同步在 limit 之前）不得回流"


# ---------------------------------------------------------------------------
# FIX-5 · 事实待确认清单覆盖注入门控四档
# ---------------------------------------------------------------------------
class TestPendingFactsCoversGate:
    def test_gate_really_has_four_flags(self):
        m = re.search(r'_FACTS_INJECT_WHERE\s*=\s*"([^"]+)"', _FACTS)
        assert m, "未找到注入门控常量"
        for flag in ("has_conflict", "is_resolved", "is_simulated", "is_stale"):
            assert flag in m.group(1)

    def test_pending_sql_covers_all_four_flags(self):
        blk = _window(_DOSSIER, "sql = (", after=520)
        for flag in ("has_conflict", "is_resolved", "is_simulated", "is_stale"):
            assert f"COALESCE({flag}" in blk, f"待确认清单漏收 {flag}"

    def test_low_confidence_is_kept_intentionally(self):
        blk = _window(_DOSSIER, "sql = (", after=520)
        assert "COALESCE(confidence, 1.0) < ?" in blk
        assert "清单 ⊇ 注入门控的补集" in _DOSSIER

    def test_old_three_flag_sql_not_returned(self):
        assert 'OR COALESCE(is_stale, 0) = 1) "\n        "ORDER BY' not in _DOSSIER.replace("\r\n", "\n")

    def test_behavior_gate_complement_is_fully_covered(self):
        """行为级：用 sqlite 验证四档补集判据能且仅能选出被门控排除的行。"""
        import sqlite3

        rows = [
            ("ok", 0, 1, 0, 0),
            ("unresolved", 0, 0, 0, 0),
            ("conflict", 1, 1, 0, 0),
            ("simulated", 0, 1, 1, 0),
            ("stale", 0, 1, 0, 1),
        ]
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE f (title TEXT, has_conflict INT, is_resolved INT, is_simulated INT, is_stale INT)")
        con.executemany("INSERT INTO f VALUES (?,?,?,?,?)", rows)
        gate = "has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0"
        pending = (
            "COALESCE(is_resolved,1)=0 OR COALESCE(has_conflict,0)=1 "
            "OR COALESCE(is_simulated,0)=1 OR COALESCE(is_stale,0)=1"
        )
        injected = {r[0] for r in con.execute(f"SELECT * FROM f WHERE {gate}")}
        listed = {r[0] for r in con.execute(f"SELECT * FROM f WHERE {pending}")}
        all_titles = {r[0] for r in rows}
        assert injected == {"ok"}
        assert listed == all_titles - injected, "清单必须覆盖门控全部排除档"
        con.close()


# ---------------------------------------------------------------------------
# FIX-6 · 三个统计失败留痕键登记
# ---------------------------------------------------------------------------
_ERROR_FLAGS = ("word_audit_error", "order_check_error", "fact_injection_summary_error")


class TestErrorFlagsInCheckpointSchema:
    def test_whitelist_contains_flags(self):
        fields = CHECKPOINT_KINDS["content_generation"][1]
        for flag in _ERROR_FLAGS:
            assert flag in fields

    def test_production_payload_really_writes_flags(self):
        for flag in _ERROR_FLAGS:
            assert f'ckpt["{flag}"] = True' in _PAYLOADS

    def test_generated_ts_in_sync(self):
        for flag in _ERROR_FLAGS:
            assert f"{flag}?:" in _SCHEMA_TS
        assert "AUTO-GENERATED" in _SCHEMA_TS

    def test_whitelist_not_dropped_by_regeneration(self):
        # parity 锁在 test_content_deep_audit::test_checkpoint_schema_matches_generated_ts
        assert CHECKPOINT_KINDS["content_generation"][0] == "content_result"


# ---------------------------------------------------------------------------
# FIX-7 · table_needed 归一收口到唯一出口
# ---------------------------------------------------------------------------
class TestTableNeededSingleExit:
    def test_both_entries_delegate_to_coerce_bool_flag(self):
        assert _PLAN.count('coerce_bool_flag(d.get("table_needed"') == 2
        assert 'tn = d.get("table_needed", False)' not in _PLAN
        assert 'tn.strip().lower() in ("true", "是", "yes", "1")' not in _PLAN

    def test_two_entries_agree_on_string_flags(self):
        """行为级：回读入口与产出入口对同一份 JSON 同结论（R211 同型防回流）。"""
        for raw, expected in (("false", False), ("TRUE ", True), ("是", True), ("no", False), (1, True)):
            d = {"table_needed": raw}
            assert ContentPlanData.from_json(d).table_needed is expected
            assert ContentPlanData.validate(d).table_needed is expected

    def test_single_exit_semantics_unchanged(self):
        assert coerce_bool_flag("FALSE") is False
        assert coerce_bool_flag(" true ") is True
