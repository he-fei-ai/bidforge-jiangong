# -*- coding: utf-8 -*-
"""R48（2026-10-06 · 图表模块深度审查）删图可观测 + 不变式加固单测。

三条修复，全部**加法式**（不传新参数即旧行为逐字一致）：

1. **删图可观测（P1）**：`build_inline_chart_plan(dropped_out=...)` 与
   `apply_inline_chart_plan(..., dropped_out=...)` 把「图表块被系统删除」逐条
   记入观测清单（reason 封闭枚举）。此前只写 backend.log，用户无从解释
   「正文里图没了、图表清单没有这张图」。
2. **不变式加固**：「某块不会登记 ⇒ 必须从正文移除」。旧实现在 3 个信封失败
   分支只 `continue` 不删块，留下正文有图、chart_predictions 无登记的幽灵图
   （当前调用图下不可达，但属结构性不变式漏洞）。
3. **僵尸图清理**：`sse_handlers` 图表登记计算异常分支此前只跳过登记，
   `apply_inline_chart_plan` 开头的 DELETE 从不执行 → 正文是新的、登记是旧的。
   现异常分支也做「仅清理」调用。
"""
import asyncio
import json
import re

import app.routers._chart_pipeline as CP
import app.routers.sse_handlers as SH
from app.routers._chart_pipeline import (
    _CHART_PER_SECTION_LIMIT,
    _CHART_SCHEME_TYPE_DEFAULT_LIMIT,
    _CHART_SCHEME_TYPE_LIMITS,
    _record_chart_drop,
    apply_inline_chart_plan,
    build_inline_chart_plan,
)

VALID_FLOW = "flowchart TD\n    A --> B\n    B --> C"
VALID_AI = json.dumps({"prompt": "现场平面布置剖面图", "title": "现场平面布置图"},
                      ensure_ascii=False)

# 删图理由封闭枚举 —— 新增理由必须同时进这个集合，否则下面的静态护栏会红。
DROP_REASONS = {
    "per_section_limit", "scheme_type_limit", "validation_failed",
    "missing_prompt", "empty_envelope", "invalid_json",
    "insert_failed", "insert_none",
}


def run(coro):
    return asyncio.run(coro)


class FakeDb:
    """最小异步 db 桩：记录 execute 调用（语义对齐 test_inline_charts.FakeDb）。"""

    def __init__(self):
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or ())))

        class _Cur:
            rowcount = 1
            lastrowid = 1
        return _Cur()

    async def commit(self):
        pass


def plan(content, **kw):
    return build_inline_chart_plan(
        "scheme-1", "sec-1", content, enforce_limits=True, **kw)


# =========================================================================
# A. _record_chart_drop 本身
# =========================================================================

class TestRecordChartDrop:

    def test_none_target_is_noop(self):
        # 不传观测清单 = 旧行为，不抛错、零副作用。
        _record_chart_drop(None, "flowchart", "per_section_limit")

    def test_single_record_shape(self):
        out = []
        _record_chart_drop(out, "gantt", "scheme_type_limit")
        assert out == [{"type": "gantt", "reason": "scheme_type_limit"}]

    def test_build_side_is_not_deduped(self):
        # build 侧按块记，同一 类型+理由 可重复（apply 侧自行去重）。
        out = []
        _record_chart_drop(out, "labor", "scheme_type_limit")
        _record_chart_drop(out, "labor", "scheme_type_limit")
        assert len(out) == 2


# =========================================================================
# B. 四条删块理由各自可观测
# =========================================================================

class TestDropReasons:

    def test_no_chart_no_drop(self):
        text = "本节正文无任何图表。"
        content, rows = plan(text)
        assert content == text
        assert rows == []

    def test_drop_list_stays_empty_when_nothing_dropped(self):
        out = []
        plan(f"```mermaid\n{VALID_FLOW}\n```", dropped_out=out)
        assert out == []

    def test_normal_chart_is_registered(self):
        out = []
        _, rows = plan(f"```mermaid\n{VALID_FLOW}\n```", dropped_out=out)
        assert len(rows) == 1
        assert out == []


class TestPerSectionLimit:

    def test_second_chart_in_one_section_is_dropped(self):
        content = "\n\n".join([f"```mermaid\n{VALID_FLOW}\n```",
                               f"```mermaid\n{VALID_FLOW}\n```"])
        new_content, rows = plan(content, dropped_out=[])
        assert len(rows) == _CHART_PER_SECTION_LIMIT == 1
        # 删掉的块必须真的从正文消失（否则是幽灵图）
        assert new_content.count("```mermaid") == 1
        out = []
        plan(content, dropped_out=out)
        assert out == [{"type": "flowchart", "reason": "per_section_limit"}]


class TestSchemeTypeLimit:

    def test_over_scheme_type_limit_is_dropped(self):
        content = f"```mermaid\n{VALID_FLOW}\n```"
        _limit = _CHART_SCHEME_TYPE_LIMITS.get("flowchart",
                                                _CHART_SCHEME_TYPE_DEFAULT_LIMIT)
        new_content, rows = plan(
            content, dropped_out=[], scheme_type_counts={"flowchart": _limit})
        assert rows == []
        assert "mermaid" not in new_content
        out = []
        plan(content, dropped_out=out, scheme_type_counts={"flowchart": _limit})
        assert out == [{"type": "flowchart", "reason": "scheme_type_limit"}]

    def test_ai_image_has_higher_scheme_limit(self):
        # ai_image 放宽到 6（对齐 OpenBidKit），其余类型默认 3。
        assert _CHART_SCHEME_TYPE_LIMITS["ai_image"] == 6
        assert _CHART_SCHEME_TYPE_DEFAULT_LIMIT == 3


class TestValidationFailed:

    def test_unfixable_chart_is_dropped_and_recorded(self):
        # 识别得出图表类型（flowchart）、但语法既不可解析也不可修复 → 删块 + 记理由。
        content = "```mermaid\nflowchart TD\n    ]invalid[[ broken\n```"
        new_content, rows = plan(content, dropped_out=[])
        assert rows == []
        assert new_content.strip() == ""
        out = []
        plan(content, dropped_out=out)
        assert out == [{"type": "flowchart", "reason": "validation_failed"}]


class TestAiImageMissingPrompt:

    def test_ai_image_without_prompt_is_dropped(self):
        payload = json.dumps({"title": "只有标题没有 prompt"}, ensure_ascii=False)
        content = f"配图如下：\n```ai_image\n{payload}\n```\n后续正文。"
        new_content, rows = plan(content, dropped_out=[])
        assert rows == []
        assert "ai_image" not in new_content
        out = []
        plan(content, dropped_out=out)
        assert out == [{"type": "ai_image", "reason": "missing_prompt"}]

    def test_ai_image_with_prompt_is_kept(self):
        content = f"配图如下：\n```ai_image\n{VALID_AI}\n```"
        out = []
        _, rows = plan(content, dropped_out=out)
        assert len(rows) == 1
        # rows 元组顺序：(id, section_id, scheme_id, chart_type, title, ...)
        assert rows[0][3] == "ai_image"
        assert out == []

# =========================================================================
# C. 不变式加固：「不登记 ⇒ 必删块」（幽灵图防线）
# =========================================================================

class TestEnvelopeFailureRemovesBlock:

    def test_ai_image_empty_envelope_removes_block(self, monkeypatch):
        # build_chart_envelope 返回空串 = 无法构造登记表 → 该块不能登记，
        # 必须从正文删除。旧实现只 continue，块留在正文里却无登记（幽灵图）。
        monkeypatch.setattr(CP, "build_chart_envelope", lambda **kw: "")
        content = f"配图如下：\n```ai_image\n{VALID_AI}\n```\n后续正文。"
        new_content, rows = plan(content, dropped_out=[])
        assert rows == []
        assert "ai_image" not in new_content, "空信封必须删块，不能留幽灵图"
        out = []
        plan(content, dropped_out=out)
        assert out == [{"type": "ai_image", "reason": "empty_envelope"}]

    def test_invalid_json_form_removes_block(self, monkeypatch):
        # 以 `{` 起首但 json.loads 失败 → 无法构造 data 分支信封。
        # ⚠️ 该分支是**防御纵深**，自然调用图下有**两层**前置守卫使它不可达：
        #   ① 扫描器 `_scan_chart_fences_full` 的 chart-json 分支自己先 json.loads
        #      （解析失败即跳过登记）；② `_validate_inline_chart` 对 JSON 形态也先
        #      json.loads，且通过后**原样返回 code**。
        # 因此要验证它，必须同时放开这两层（外加让信封构造返回非空值，否则走的是
        # `empty_envelope` 而不是 `invalid_json`）。这不是给分支制造漏洞，而是把
        # 「未来某次改动绕过扫描器校验」时的兜底行为固定下来：仍必须删块。
        monkeypatch.setattr(CP, "build_chart_envelope", lambda **kw: '{"ok": true}')
        monkeypatch.setattr(CP, "_validate_inline_chart",
                            lambda ct, code: (True, code))
        monkeypatch.setattr(
            CP, "_scan_chart_fences_full",
            lambda content: [("gantt", '{"type": "gantt"', 0)])
        content = "```chart-json\n{\"type\": \"gantt\"\n```\n后续正文。"
        new_content, rows = plan(content, dropped_out=[])
        assert rows == []
        assert "chart-json" not in new_content, "非法 JSON 必须删块"
        out = []
        plan(content, dropped_out=out)
        assert out == [{"type": "gantt", "reason": "invalid_json"}]

    def test_empty_mermaid_envelope_removes_block(self, monkeypatch):
        monkeypatch.setattr(CP, "build_chart_envelope", lambda **kw: "")
        content = f"流程如下图所示：\n```mermaid\n{VALID_FLOW}\n```\n后续正文。"
        new_content, rows = plan(content, dropped_out=[])
        assert rows == []
        assert "mermaid" not in new_content
        out = []
        plan(content, dropped_out=out)
        assert out == [{"type": "flowchart", "reason": "empty_envelope"}]


# =========================================================================
# D. apply_inline_chart_plan：锁内复核也要上报 + 仅清理路径
# =========================================================================

class _ExecutedCur:
    rowcount = 1
    lastrowid = 1


class TestApplyPlanDropObservability:

    def _row(self, chart_type="gantt"):
        return ("id-1", "sec-1", "scheme-1", chart_type, "purpose", 5,
                "generated", '{"data": {}}')

    def test_insert_limit_skip_is_recorded(self):
        # db 返回 0 行 → live={}；本批内累积超过同类型限额的部分会被复核跳过。
        _limit = _CHART_SCHEME_TYPE_LIMITS.get("gantt",
                                                _CHART_SCHEME_TYPE_DEFAULT_LIMIT)
        rows = [self._row() for _ in range(_limit + 2)]
        out = []
        run(apply_inline_chart_plan(
            FakeDb(), "sec-1", rows, None, dropped_out=out))
        assert len(out) == 1
        assert out[0] == {"type": "gantt", "reason": "scheme_type_limit"}

    def test_apply_side_dedups_same_type_and_reason(self):
        # apply 侧按「类型+理由」去重：同一类型连续失败只记一条 —— 否则
        # charts_dropped_count 会虚高（skipped_types 本身按类型去重）。
        db = FakeDb()

        async def _boom(self, sql, params=None):
            if sql.strip().startswith("INSERT"):
                raise RuntimeError("simulated transaction conflict")
            return _ExecutedCur()

        _orig = FakeDb.execute
        FakeDb.execute = _boom
        try:
            out = []
            run(apply_inline_chart_plan(
                db, "sec-1", [self._row() for _ in range(4)], None, dropped_out=out))
        finally:
            FakeDb.execute = _orig
        assert [o["reason"] for o in out] == ["insert_failed"]
        assert len(out) == 1

    def test_empty_rows_only_deletes(self):
        # 「仅清理」路径：rows=[] 时只执行 DELETE、不写正文 —— 这是
        # sse_handlers 图表登记计算失败时的僵尸图清理调用形态。
        db = FakeDb()
        assert run(apply_inline_chart_plan(db, "sec-9", [], None)) is None
        assert any(s.strip().startswith("DELETE FROM chart_predictions")
                   for s, _ in db.executed)
        assert not any(s.strip().startswith("INSERT") for s, _ in db.executed)

    def test_empty_rows_without_content_still_deletes(self):
        db = FakeDb()
        run(apply_inline_chart_plan(db, "sec-9", [], None, dropped_out=[]))
        assert any(s.strip().startswith("DELETE FROM chart_predictions")
                   for s, _ in db.executed)

    def test_no_dropped_out_means_zero_overhead(self):
        # 不传 dropped_out = 旧行为，_note_drop → _record_chart_drop(None,...) 空转。
        db = FakeDb()
        assert run(apply_inline_chart_plan(db, "sec-1", [self._row()], None)) is None



# =========================================================================
# E. 静态护栏：新增删块出口必须成对「删块 + 记理由」
# =========================================================================

_PIPELINE_SRC = open(CP.__file__, encoding="utf-8").read()


def _build_plan_body():
    i = _PIPELINE_SRC.index("def build_inline_chart_plan(")
    j = _PIPELINE_SRC.index("\nasync def apply_inline_chart_plan(", i)
    return _PIPELINE_SRC[i:j]


def _apply_plan_body():
    i = _PIPELINE_SRC.index("async def apply_inline_chart_plan(")
    j = _PIPELINE_SRC.index("async def register_inline_charts(", i)
    return _PIPELINE_SRC[i:j]


def test_every_drop_record_site_has_known_reason():
    reasons = set(re.findall(
        r'_record_chart_drop\([^,]+,\s*[^,]+,\s*"([^"]+)"\)', _build_plan_body()))
    assert reasons, "未找到任何 _record_chart_drop 调用"
    unknown = reasons - DROP_REASONS
    assert not unknown, f"出现未登记的删图理由（绕过前端/报告口径）：{sorted(unknown)}"


def test_apply_plan_dedup_reasons_are_known():
    reasons = set(re.findall(r'_note_drop\(ct,\s*"([^"]+)"\)', _apply_plan_body()))
    unknown = reasons - DROP_REASONS
    assert not unknown, f"锁内复核出现未登记的删图理由：{sorted(unknown)}"


def test_every_envelope_empty_branch_deletes_the_block():
    """不变式锁：`if not payload_json` / `if not _payload` 分支必须删块。

    旧实现这三处只 `continue`，块留在正文却无登记（幽灵图，且绕过每章≤1 与
    同类型≤3 限额）。
    ⚠️ 判据必须锚定**该分支自己的代码块**，不能取固定长度的窗口 —— 否则相邻
    分支（例如 `if not _prompt:` 里那条 `edits[ordinal] = None`）会把窗口填满，
    删掉目标分支的赋值仍通过护栏（假通过）。此处按缩进切出分支体，剥掉注释行
    后再判定（注释里出现 `edits[` 也不算）。
    """
    body = _build_plan_body()
    for anchor in ("if not _payload:", "if not payload_json:"):
        assert anchor in body, f"缺少信封判空分支：{anchor}"
        start = body.index(anchor)
        rest = body[start:]
        # 到下一个同级（8 空格）语句为止即为本分支体
        m = re.search(r"\n        (?:if |else |for |while |return\b)", rest[len(anchor):])
        block = rest[len(anchor):m.start() + 8] if m else rest[len(anchor):]
        code_only = "\n".join(
            ln for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#"))
        assert "edits[ordinal] = None" in code_only, (
            f"分支 {anchor!r} 内没有删块赋值 —— 该块会留在正文却无 chart_predictions "
            "登记（幽灵图，且绕过每章≤1 与同类型≤3 限额）")


def test_all_four_drop_exits_are_wired():
    """既有删块出口（每章超限/同类型超限/校验失败/缺 prompt）不得被摘掉接线。"""
    body = _build_plan_body()
    for reason in ("per_section_limit", "scheme_type_limit",
                   "validation_failed", "missing_prompt"):
        assert f'"{reason}"' in body, f"删图理由 {reason} 的接线被移除"


def test_signatures_expose_dropped_out():
    assert "dropped_out: list | None = None" in _build_plan_body()
    assert "dropped_out: list | None = None" in _apply_plan_body()


def test_per_section_limit_semantics_documented():
    """「每章≤1」的真实口径是 per-section_id（叶子小节），口径说明不得被删。

    一个一级章下挂 N 个二级小节时可各自合法产出 1 张 ⇒ 该章最多 N 张。
    本护栏防止后人「顺手按一级章计数」造成行为变更（收紧限额会删掉合法图表）。
    """
    body = _build_plan_body()
    assert "_CHART_PER_SECTION_LIMIT" in body
    assert "每个 section_id" in body, (
        "配图上限的口径说明被删掉 —— 提示词的「每章≤1」与代码的 per-section "
        "计数是两回事，必须写明以免后人误改行为")


# =========================================================================
# F. sse_handlers 接线（跨文件静态锁，防静默摘除）
# =========================================================================

_SSE_SRC = open(SH.__file__, encoding="utf-8").read()


def test_sse_handlers_passes_dropped_out_to_both_plan_functions():
    assert re.search(r'build_inline_chart_plan\([^)]*dropped_out=\s*_chart_dropped',
                     _SSE_SRC, re.S), (
        "build_inline_chart_plan 调用未传 dropped_out —— 删图将再次零可观测")
    assert re.search(r'apply_inline_chart_plan\([^)]*dropped_out=\s*_chart_dropped',
                     _SSE_SRC, re.S), (
        "apply_inline_chart_plan 调用未传 dropped_out —— 锁内复核删图再次不可见")


def test_charts_dropped_is_written_into_report():
    assert '"charts_dropped"' in _SSE_SRC
    assert '"charts_dropped_count"' in _SSE_SRC


def test_report_json_redumped_after_drop_collection():
    """`report_json` 在锁之前 dump 一次；删图汇总后**必须再 dump 一次**。

    否则 charts_dropped 永远不会出现在落库的 report_json 里（加了键但没生效）。
    """
    i = _SSE_SRC.index('"charts_dropped"')
    after = _SSE_SRC[i:]
    assert "json.dumps(report, ensure_ascii=False)" in after, (
        "charts_dropped 写入 report 后未重新 json.dumps —— 落库的 report_json "
        "不含删图信息（护栏失效，用户仍看不到理由）")


def test_zombie_chart_cleanup_branch_exists():
    """图表登记计算异常时必须做「仅清理」调用，否则残留上一轮旧登记（僵尸图）。"""
    assert re.search(
        r'await\s+apply_inline_chart_plan\(\s*db,\s*section_id,\s*\[\],\s*None\s*\)',
        _SSE_SRC), (
        "sse_handlers 缺少图表登记计算失败的仅清理调用 —— 正文落库而 "
        "chart_predictions 残留上一轮旧值，导出会追加已不在正文里的图")
    # 清理失败不得阻断正文落库
    assert "图表登记清理失败" in _SSE_SRC


def test_lead_in_constants_have_single_source():
    """引导语长度上限必须有唯一事实来源（R48 修复 1 的静态锁）。

    旧实现散着 3 处裸 `60` + 两份长度常量 + 两份正则；此处锁定
    content_blocks 是唯一真值，_chart_pipeline / export 均为别名或 import。
    """
    import app.services.content_blocks as CB
    import app.routers.export as EX
    assert CB.LEAD_IN_MAX_CHARS == 60
    assert CP.LEAD_IN_MAX_CHARS is CB.LEAD_IN_MAX_CHARS
    assert CB._LEAD_IN_MAX_CHARS is CB.LEAD_IN_MAX_CHARS
    assert hasattr(CB, "ORPHAN_LEAD_IN_RE")
    src = open(EX.__file__, encoding="utf-8").read()
    assert "len(text) > LEAD_IN_MAX_CHARS" in src, (
        "export 侧引导语长度判定应引用 LEAD_IN_MAX_CHARS，不得回退为裸 60")

