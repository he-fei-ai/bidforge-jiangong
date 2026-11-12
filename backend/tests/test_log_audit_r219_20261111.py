# -*- coding: utf-8 -*-
"""R219（2026-11-11）后台日志驱动深度分析与功能轨迹诊断护栏。

动因（``logs/backend.log``，2026-11-07 22:45 ~ 11-11 20:19，2658 行 / 141 WARN / 6 ERROR）：

① 6 条 ``GET /api/v1/system/activity/stream -> 200 (26483691ms)`` 等
   30min~7h21m 的「耗时」实为 SSE 连接生命周期，与普通请求混在同一
   ``(Nms)`` 口径 ⇒ P95/P99 基线失真（实测 P99 被推到 120000ms）、
   慢请求不可定位；且旧计时用墙钟 ``time.time()``，NTP 校准会污染差值。

② 6 条「上下文截断（危大判定匹配文本）：3725 → 2000 字」⇒ 头部截断
   吞掉排在后面的事实名称（如「基坑开挖深度」）。``classify_scheme``
   只靠这段文本做 ``match_category_keywords`` 子类命中；子类不命中时
   ``evaluate_hazard_level`` 连阈值规则都不评估 ⇒ 即便结构化 ``params``
   里已有 ``depth=16``，SAF-08 也静默放行（安全方向漏判）。

手法：行为实证（ASGI 驱动 / 真实判定链）+ **反证**（锁住「修前确实漏判」
这一前提，防止护栏变成恒真）+ 源码棘轮（防旧实现回流）+ 工具贯通。
"""
from __future__ import annotations

import importlib.util
import logging
import pathlib

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
MW_PATH = APP_DIR / "middleware.py"


class _StubApp:
    """最小 ASGI 应用：固定状态码 / 响应体 / content-type（零依赖）。"""

    def __init__(self, status: int = 200, body: bytes = b"{}", content_type: bytes = b"application/json"):
        self.status = status
        self.body = body
        self.content_type = content_type

    async def __call__(self, scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", self.content_type)],
            }
        )
        await send({"type": "http.response.body", "body": self.body})


async def _drive(app, path: str = "/api/v1/schemes/x") -> list:
    from app.middleware import RequestLogMiddleware

    mw = RequestLogMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def _access_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "->" in r.getMessage() and "[trace=" in r.getMessage()]


# =====================================================================
# A 组 · 流式访问日志口径（FIX-1）
# =====================================================================
class TestStreamAccessLog:
    @pytest.mark.asyncio
    async def test_normal_request_log_stays_verbatim(self, caplog):
        """普通请求访问日志逐字不变（R178 F-13 格式护栏不放松）。"""
        with caplog.at_level(logging.INFO, logger="http"):
            await _drive(_StubApp())
        logs = _access_logs(caplog)
        assert logs, "访问日志必须仍在"
        assert "GET /api/v1/schemes/x -> 200 (" in logs[0]
        assert "[trace=" in logs[0]
        assert "kind=stream" not in logs[0], "普通请求不得被误判为流式"

    @pytest.mark.asyncio
    async def test_sse_response_marked_as_stream(self, caplog):
        """SSE 响应必须加法式带上 kind=stream 与可读连接时长。"""
        with caplog.at_level(logging.INFO, logger="http"):
            await _drive(
                _StubApp(content_type=b"text/event-stream; charset=utf-8"),
                path="/api/v1/system/activity/stream",
            )
        logs = _access_logs(caplog)
        assert logs
        assert "GET /api/v1/system/activity/stream -> 200 (" in logs[0]
        assert "kind=stream" in logs[0], "SSE 必须与普通请求可分离统计"
        assert "conn=" in logs[0]
        assert "[trace=" in logs[0]

    @pytest.mark.asyncio
    async def test_stream_failure_reason_still_logged(self, caplog):
        """流式 4xx/5xx 仍必须补记失败原因（不改 R178 既有语义）。"""
        with caplog.at_level(logging.INFO, logger="http"):
            await _drive(
                _StubApp(status=503, body=b'{"detail":"busy"}', content_type=b"text/event-stream"),
                path="/api/v1/sse/tasks",
            )
        assert any("失败原因" in r.getMessage() and "busy" in r.getMessage() for r in caplog.records)

    def test_stream_duration_format_matches_production_sample(self):
        """生产最长样本 26483691ms 必须渲染为可读的 7h21m23s。"""
        from app.middleware import _format_stream_duration

        assert _format_stream_duration(26483691) == "7h21m23s"
        assert _format_stream_duration(0) == "0s"
        assert _format_stream_duration(59_000) == "59s"
        assert _format_stream_duration(61_000) == "1m01s"
        assert _format_stream_duration(3_661_000) == "1h01m01s"

    def test_stream_duration_never_raises_on_degenerate_input(self):
        """格式化失败绝不能影响请求日志（负值 / None / NaN / 字符串）。"""
        from app.middleware import _format_stream_duration

        for bad in (-1, None, float("nan"), "x", float("inf")):
            assert isinstance(_format_stream_duration(bad), str)

    def test_cost_uses_monotonic_clock(self):
        """源码棘轮：耗时测量不得退回墙钟 time.time()。"""
        src = MW_PATH.read_text(encoding="utf-8")
        assert "start = time.monotonic()" in src
        assert "cost = (time.monotonic() - start) * 1000" in src
        assert "cost = (time.time() - start) * 1000" not in src


# =====================================================================
# B 组 · 危大判定截断免疫（FIX-2，安全方向）
# =====================================================================
def _facts_with_late_hazard() -> list[dict]:
    """30 条无危大关键词的长事实 + 1 条压在末尾的「基坑开挖深度 16m」。"""
    return [{"name": "工程概况描述", "value": "现场情况说明" * 40} for _ in range(30)] + [
        {"name": "基坑开挖深度", "fact_key": "excavation_depth", "value": "16m", "value_unit": "m"}
    ]


def _legacy_joined(facts: list[dict]) -> str:
    """修复前的拼串口径（名称+值，无关键词前置）。"""
    return " ".join((f.get("name") or "") + " " + (f.get("value") or "") for f in facts)


class TestHazardClipImmunity:
    def test_legacy_head_clip_really_drops_subcategory(self):
        """反证：旧口径确实会吞掉危大关键词并让阈值规则零命中。

        没有这条反证，「修复后报出 SAF-08」可能只是恒真断言；
        这里锁住「漏判真实存在」这一前提。
        """
        from app.services.facts_classification import danger_check
        from app.services.prompt_material_guard import clip_text_logged

        facts = _facts_with_late_hazard()
        old = clip_text_logged(_legacy_joined(facts), 2000, "护栏反证")
        assert len(old) == 2000
        assert "开挖" not in old, "反证前提：危大关键词必须确实被头部截断吞掉"
        cls = danger_check("某住宅楼施工组织设计", facts, extra_text=old)["classification"]
        assert not [h for h in cls["hazards"] if h.get("hazard_reasons")], (
            "反证前提：旧口径下子类不命中，阈值规则不被评估（SAF-08 漏判）"
        )

    def test_fixed_path_reports_saf08(self):
        """同一份事实经 check_hazard_params 必须报出 SAF-08 且带真实阈值证据。"""
        from app.services.preflight_engine import PreflightContext, check_hazard_params

        out = check_hazard_params(
            PreflightContext(scheme_name="某住宅楼施工组织设计", facts=_facts_with_late_hazard())
        )
        assert [f["rule_id"] for f in out] == ["SAF-08"]
        assert any("depth=16" in e for e in (out[0].get("evidence") or [])), "必须带真实阈值参数证据"

    def test_hints_all_from_registry_single_source(self):
        """关键词提示必须逐项来自 HAZARD_CATEGORIES，不得复制第二份词表。"""
        from app.services.preflight_engine import _hazard_keyword_hints
        from app.services.scheme_classification import HAZARD_CATEGORIES

        all_kw = {
            kw
            for cat in HAZARD_CATEGORIES
            for sub in cat.get("subs", []) or []
            for kw in sub.get("keywords", ()) or ()
        }
        hints = _hazard_keyword_hints(_facts_with_late_hazard()).split()
        assert hints, "末尾事实里的危大关键词必须被提取出来"
        assert set(hints) <= all_kw
        assert "开挖" in hints or "基坑开挖" in hints

    def test_no_hints_falls_back_to_legacy_text(self):
        """无危大关键词 / 脏数据 → 提示为空，extra_text 逐字回退旧行为。"""
        from app.services.preflight_engine import _hazard_keyword_hints

        assert _hazard_keyword_hints([{"name": "工程概况", "value": "说明"}]) == ""
        assert _hazard_keyword_hints([]) == ""
        assert _hazard_keyword_hints([None, "bad", {"name": "x"}]) == ""

    def test_extra_text_budget_not_inflated(self):
        """提示前置后总预算仍为 2000 字，source_text 不得膨胀。"""
        from app.services.preflight_engine import _hazard_keyword_hints
        from app.services.prompt_material_guard import clip_text_logged

        facts = _facts_with_late_hazard()
        hints = _hazard_keyword_hints(facts)
        text = clip_text_logged(hints + " " + _legacy_joined(facts), 2000, "护栏预算")
        assert len(text) <= 2000
        assert "开挖" in text[:100], "类目关键词提示必须落在保留区最前部"

    def test_long_keyword_beats_short_common_words_to_reachable_zone(self):
        """R220：命中关键词必须长者优先，短高频词不得把长关键词挤到保留区外。

        场景：30 条「挖孔桩施工」（命中短关键词「挖孔」/「人工挖孔」）+
        1 条末尾的「基坑支护与降水」（命中长关键词「基坑支护」4 字、
        中关键词「支护」「降水」2 字）。按长度降序则「人工挖孔」「基坑支护」
        （4 字）必须领先 2 字关键词。
        """
        from app.services.preflight_engine import _hazard_keyword_hints

        facts = (
            [{"name": "挖孔桩施工", "value": "人工挖孔 1 处"} for _ in range(30)]
            + [{"name": "基坑支护与降水", "value": "16m"}]
        )
        hints = _hazard_keyword_hints(facts).split()
        assert "基坑支护" in hints, "4 字长关键词必须进提示串"
        assert "人工挖孔" in hints, "4 字关键词必须进提示串"
        # 4 字关键词必须领先 2 字关键词（保截断保留区）
        assert hints.index("基坑支护") < hints.index("支护"), "长关键词必须排在短高频词之前"
        assert hints.index("人工挖孔") < hints.index("支护")

    def test_value_phrase_keywords_reach_hints(self):
        """R220：类目证据藏在 value 整句里（name/fact_key 无关键词）也必须进提示串。

        「16m」这类纯数值事实的类目词常以整句形态出现在 value（R193 起 value
        经 value_to_markdown 归一），旧版只拼 name+fact_key 会漏提。
        """
        from app.services.preflight_engine import _hazard_keyword_hints

        facts = [
            {"name": "深度", "value": "基坑开挖深度 16m"},
        ]
        hints = _hazard_keyword_hints(facts).split()
        assert "基坑开挖" in hints, "value 整句里的类目关键词（4 字长）必须被提取"
        assert "开挖" in hints, "同族的 2 字短关键词也必须进提示串"

    def test_name_based_hazard_still_short_circuits(self):
        """名称口径已判危大时不得重复报（既有短路语义零变化，防误报）。"""
        from app.services.preflight_engine import PreflightContext, check_hazard_params

        out = check_hazard_params(
            PreflightContext(scheme_name="基坑开挖专项施工方案", facts=_facts_with_late_hazard())
        )
        assert out == []


# =====================================================================
# C 组 · 日志分析工具贯通（基线可复算）
# =====================================================================
class TestAuditToolParity:
    @staticmethod
    def _load():
        spec = importlib.util.spec_from_file_location(
            "log_audit_r219", ROOT_DIR / "scripts" / "log_audit_r219.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_time_regex_does_not_misread_english_word_as_seconds(self):
        """聚类工具不得把 sections=739 schemes=6 里的 s 当成「秒」。"""
        mod = self._load()
        assert not mod.TIME_RE.search("sections=739 schemes=6")
        assert mod.TIME_RE.search("-> 200 (44ms)")

    def test_kind_stream_marker_survives_normalization(self):
        """新增的流式标记在模板归一化后仍可区分（基线可分离统计）。"""
        mod = self._load()
        tpl = mod.normalize(
            "GET /api/v1/system/activity/stream -> 200 (26483691ms) "
            "[trace=fb12bf83fe69] kind=stream conn=7h21m23s"
        )
        assert "kind=stream" in tpl
        assert "conn=" in tpl

    def test_sse_tasks_polling_is_not_stream(self):
        """棘轮：每 3s 的 /sse/tasks 轮询不得被误判为流式长连接。

        本轮首版聚类用 ``"/sse/"`` 粗判，把 759 条 21~78ms 的轮询算成
        长连接并得出「流式 P50=11ms」的假基线；这里锁死精确判据。
        """
        mod = self._load()
        assert mod.is_stream_request("GET /api/v1/sse/tasks -> 200 (54ms) [trace=x]") is False
        assert mod.is_stream_request("GET /api/v1/system/activity/stream -> 200 (26483691ms) [trace=x]") is True
        assert mod.is_stream_request("POST /api/v1/sse/generate-outline/s1 -> 200 (587490ms) [trace=x]") is True
        assert mod.is_stream_request("GET /api/v1/sse/tasks -> 200 (54ms) [trace=x] kind=stream conn=0s") is True

    def test_real_slow_requests_stay_in_baseline(self):
        """真实慢请求（导出/审核落库/预检）不得被当作长连接排除。"""
        mod = self._load()
        for msg in (
            "POST /api/v1/schemes/s1/export/docx -> 200 (21400ms) [trace=x]",
            "POST /api/v1/compliance/check -> 200 (12000ms) [trace=x]",
            "POST /api/v1/schemes/s1/review/autofix/stage -> 200 (15000ms) [trace=x]",
        ):
            assert mod.is_stream_request(msg) is False, msg
