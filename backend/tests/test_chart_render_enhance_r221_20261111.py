"""R221（2026-11-11）· 图表导出渲染增强护栏。

承接外部工具报告 `chart-render-fix-report/chart-render-fix-report.md`：
报告把 `allow_pil_fallback` 默认值由 False 改为 True 后即宣告「已修复」，但
亲验发现两处事实错误 + 一批未覆盖的同类丢图：

  ① 报告称「gantt 是唯一走 Mermaid 语法路径的图表类型」—— 错。
     提示词/校验器/映射表还放行 flowchart、pie/xychart(comparison)、
     timeline、architecture(-beta)，以及 12 类 HTTP-only 类型。
  ② 报告称「开启 PIL 兜底即可保证图表出现在成稿中」—— 错。
     PIL v2 只对 4 类语法载荷有解析器；architecture/labor/layout 的语法载荷
     与 12 类 HTTP-only 类型在 HTTP 不可用时**仍然必返回 None**。

本轮把「可渲染性」收口成 `chart_render_capability(类型 × 载荷形态)` 单一事实源，
并补齐报告 §6.1 的两项短期建议（预检就绪度 / PIL 兜底计数）。本文件锁定
下列不变式，防止后续会话把判据改回「只按类型名猜」。

覆盖分组：
  A 能力矩阵：7 类 JSON / 4 类语法 / architecture 语法 / 12 类 HTTP-only
  B 默认值单一事实源 + 前后端 parity
  C 预检就绪度纯函数（混合载荷 / 脏数据 / 探测失败降级）
  D 渲染策略出参（含「不传 strategy_out 时旧 fake 逐字兼容」回归锁）
  E 导出侧失败归因端到端（architecture 语法 / 非甘特 JSON / 开关关闭 / 兜底计数）
"""

from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from app.services.chart_validators import (
    PIL_RENDERABLE_CHART_TYPES,
    PIL_SYNTAX_CHART_TYPES,
    chart_render_capability,
)
from fastapi.responses import FileResponse

_WS_ROOT = Path(__file__).resolve().parents[2]


def _real_png() -> bytes:
    """R199 教训：_chart_probe 会做 PIL verify，假字节会被正确拒收。"""
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (32, 24), "white").save(buf, format="PNG")
    return buf.getvalue()


# ===========================================================================
# A · 能力矩阵（报告 §3.3 的错误结论在此被逐条钉死）
# ===========================================================================


def test_syntax_types_is_strict_subset_of_pil_renderable_types():
    assert PIL_SYNTAX_CHART_TYPES < PIL_RENDERABLE_CHART_TYPES
    assert PIL_SYNTAX_CHART_TYPES == {"flowchart", "gantt", "comparison", "timeline"}


@pytest.mark.parametrize("ct", sorted(PIL_RENDERABLE_CHART_TYPES))
def test_all_json_payloads_are_pil_native_without_http(ct):
    cap = chart_render_capability(ct, '{"type":"%s","steps":[]}' % ct)
    assert cap == {"payload": "json", "pil": "native", "http": False}, (ct, cap)


@pytest.mark.parametrize(
    ("ct", "code"),
    [
        ("flowchart", "flowchart TD\n A-->B"),
        ("gantt", "gantt\n title 进度\n dateFormat YYYY-MM-DD\n 准备 :2026-01-01, 3d"),
        ("comparison", "pie title 占比\n 甲 : 40\n 乙 : 60"),
        ("timeline", "timeline\n title 里程碑\n 2026-01 : 开工"),
    ],
)
def test_four_syntax_payloads_are_locally_recoverable(ct, code):
    """报告断言「只有 gantt 走语法路径」—— 这 4 类全部会走。"""
    cap = chart_render_capability(ct, code)
    assert cap == {"payload": "mermaid", "pil": "syntax", "http": True}, (ct, cap)


@pytest.mark.parametrize("ct", ["architecture", "labor", "layout"])
def test_json_only_types_have_no_syntax_parser(ct):
    """R221 实锤：类型在 7 类白名单内，但语法载荷下 PIL 画不了。

    旧 `_note_no_pil_fallback` 只按类型白名单反向判定 → 这三类静默漏报。
    """
    cap = chart_render_capability(ct, "graph TD\n A-->B")
    assert cap["pil"] == "unsupported", (ct, cap)
    assert cap["http"] is True


@pytest.mark.parametrize(
    "ct",
    ["sequence", "class", "state", "er", "journey", "mindmap", "quadrant", "git", "sankey", "block", "kanban"],
)
def test_http_only_types_are_unrenderable_locally(ct):
    cap = chart_render_capability(ct, "flowchart TD\n A-->B")
    assert cap["pil"] == "unsupported", (ct, cap)


def test_unknown_or_empty_type_is_never_assumed_renderable():
    assert chart_render_capability("", "graph TD\n A-->B")["pil"] == "unsupported"
    assert chart_render_capability("  GANTT  ", "gantt\n title x")["pil"] == "syntax"


def test_array_and_whitespace_prefixed_json_payloads_are_json():
    assert chart_render_capability("flowchart", "  \n [1,2]")["payload"] == "json"
    assert chart_render_capability("flowchart", "  \n {bad")["payload"] == "json"


# ===========================================================================
# B · 默认值单一事实源 + 前后端 parity
# ===========================================================================


def test_pil_fallback_default_is_true_and_shared_with_frontend():
    from app.routers import export as export_router

    assert export_router.ALLOW_PIL_FALLBACK_DEFAULT is True
    frontend = (_WS_ROOT / "frontend/src/pages/schemeWorkbench/tabs/ExportTab.tsx").read_text(encoding="utf-8")
    assert re.search(r"allow_pil_fallback:\s*true", frontend), "前端导出表单初值必须与后端默认值同值"


def test_render_stage_reads_default_from_single_source():
    """源码锁：渲染段默认值必须引用 ALLOW_PIL_FALLBACK_DEFAULT 单一常量。

    ✅ R233（2026-11-13）重锚（意图保留、强度不减）：旧断言锁「实现形态」
    `config.get(k, DEFAULT)`；R233 FIX-4 把读取改为显式 None 归一
    （`DEFAULT if _apf is None else _apf`）—— R230 A6 同款收尾（显式 None
    与「未传」必须同语义，指纹归一把 None 视同未传，旧 get(k, default) 对
    「键存在值为 None」得 None falsy，同一指纹两种渲染行为）。单一常量引用
    与「不得写死第二份字面量」的意图不变，断言改为同时锁：
    ① 常量引用在位；② get 第二参形态（None 不传默认）不得回流。
    """
    src = (_WS_ROOT / "backend/app/routers/export.py").read_text(encoding="utf-8")
    assert "ALLOW_PIL_FALLBACK_DEFAULT if _apf is None else _apf" in src, "R233 None 归一形态在位"
    assert src.count("ALLOW_PIL_FALLBACK_DEFAULT") >= 1
    # 旧双参形态（get(k, default) 对显式 None 直通 falsy）不得回流；
    # 无第二参的 config.get(k)（配合显式 None 归一）是合法形态，不禁。
    assert 'config.get("allow_pil_fallback", ALLOW_PIL_FALLBACK_DEFAULT)' not in src, (
        "旧 get(k, default) 双参形态不得回流（None 直通分叉）"
    )


# ===========================================================================
# C · 预检就绪度（报告 §6.1 短期建议①）
# ===========================================================================


def test_readiness_counts_syntax_payloads_and_unrenderable_ones(monkeypatch):
    from app.routers.export import _chart_render_readiness

    class _FakeClient:
        @staticmethod
        def is_available():
            return False

        @staticmethod
        def all_backends_probed_unavailable():
            return True

    monkeypatch.setattr(
        "app.services.ai.mermaid_service.get_mermaid_service_client", lambda: _FakeClient()
    )
    rows = [
        {"chart_type": "gantt", "data_json": json.dumps({"mermaid_code": "gantt\n title 进度"})},
        {"chart_type": "architecture", "data_json": json.dumps({"mermaid_code": "architecture-beta\n service A"})},
        {"chart_type": "flowchart", "data_json": json.dumps({"type": "flowchart", "steps": ["a", "b", "c"]})},
        {"chart_type": "mindmap", "data_json": "mindmap\n root((X))\n  A"},
        {"chart_type": "", "data_json": ""},
    ]
    out = _chart_render_readiness(rows)
    assert out["http_service_available"] is False
    assert out["all_backends_unavailable"] is True
    assert out["syntax_payload_count"] == 3, out
    assert out["unrenderable_syntax_count"] == 2, out
    assert set(out["unrenderable_syntax_types"]) == {"architecture", "mindmap"}, out
    assert out["pil_fallback_default"] is True


def test_readiness_degrades_silently_when_probe_or_rows_break(monkeypatch):
    from app.routers.export import _chart_render_readiness

    def _boom():
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr("app.services.ai.mermaid_service.get_mermaid_service_client", _boom)
    out = _chart_render_readiness([{"chart_type": "gantt", "data_json": "gantt\n title x"}, object()])
    assert out["http_service_available"] is None
    assert out["all_backends_unavailable"] is False
    assert out["syntax_payload_count"] == 1, out
    assert out["unrenderable_syntax_count"] == 0


def test_readiness_ignores_dirty_data_json():
    from app.routers.export import _chart_render_readiness

    out = _chart_render_readiness([{"chart_type": "gantt", "data_json": "{not json"}])
    # 损坏的 JSON 仍是「以 { 开头」的数据载荷，不应被误算成语法载荷
    assert out["syntax_payload_count"] == 0, out


# ===========================================================================
# D · 渲染策略出参（报告 §6.1 短期建议②）
# ===========================================================================


def test_strategy_out_reports_none_for_empty_input():
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes

    out: dict = {}
    assert render_mermaid_to_bytes("", "gantt", skip_http=True, strategy_out=out) is None
    assert out["strategy"] == "none", out


def test_strategy_out_reports_pil_syntax_fallback(monkeypatch):
    """HTTP 失败后语法载荷由 PIL 出图 → 必须记为兜底（不是原生）。"""
    from app.services.ai import mermaid_renderer as mr

    monkeypatch.setattr(mr, "get_mermaid_service_client", lambda *a, **k: _UnavailableClient())
    monkeypatch.setattr(mr, "_render_flowchart_image_v2", lambda code: BytesIO(_real_png()))
    out: dict = {}
    res = mr.render_mermaid_to_bytes(
        "flowchart TD\n A[准备] --> B[施工]", "flowchart", skip_http=True, strategy_out=out
    )
    assert res is not None
    assert out["strategy"] == "pil_syntax_fallback", out


def test_strategy_out_reports_pil_json_for_json_payload(monkeypatch):
    """JSON 载荷走的是 PIL 唯一原生路径，绝不能被记成「兜底」。"""
    from app.services.ai import mermaid_renderer as mr

    monkeypatch.setattr(mr, "get_mermaid_service_client", lambda *a, **k: _UnavailableClient())
    monkeypatch.setattr(mr, "_render_comparison_image_v2", lambda data: BytesIO(_real_png()))
    payload = json.dumps(
        {"type": "comparison", "title": "R221 唯一载荷", "items": [{"name": "甲", "value": 40}]}
    )
    out: dict = {}
    res = mr.render_mermaid_to_bytes(payload, "comparison", skip_http=True, strategy_out=out)
    assert res is not None
    assert out["strategy"] == "pil_json", out


def test_get_or_render_without_strategy_never_passes_new_kwarg(monkeypatch, tmp_path):
    """回归锁：旧调用方 / monkeypatch fake 的签名逐字兼容。

    首次实现无条件透传 strategy_out，定向回归当场红了 4 个既有用例
    （fake_render 不接受新 kwargs）。此处把「条件透传」钉死。
    """
    from app.services.ai import mermaid_renderer as mr

    seen: dict = {}

    def strict_fake(code, chart_type, *, skip_http=False, allow_pil=True):
        seen["skip_http"] = skip_http
        return BytesIO(_real_png())

    monkeypatch.setattr(mr, "render_mermaid_to_bytes", strict_fake)
    cache = mr.ChartCache(cache_dir=str(tmp_path))
    assert cache.get_or_render("graph TD\n A-->B", "flowchart", skip_http=True) is not None
    assert seen["skip_http"] is True


def test_get_or_render_passes_strategy_when_requested(monkeypatch, tmp_path):
    from app.services.ai import mermaid_renderer as mr

    out: dict = {}

    def fake(code, chart_type, *, skip_http=False, allow_pil=True, strategy_out=None):
        if isinstance(strategy_out, dict):
            strategy_out["strategy"] = "http"
        return BytesIO(_real_png())

    monkeypatch.setattr(mr, "render_mermaid_to_bytes", fake)
    cache = mr.ChartCache(cache_dir=str(tmp_path))
    assert cache.get_or_render("graph TD\n A-->B", "flowchart", skip_http=True, strategy_out=out) is not None
    assert out["strategy"] == "http", out


class _UnavailableClient:
    @staticmethod
    def all_backends_probed_unavailable():
        return True

    @staticmethod
    def render_mermaid_via_http(*_a, **_k):
        raise AssertionError("HTTP 不可用时不应发起渲染请求")


# ===========================================================================
# E · 导出侧失败归因端到端
# ===========================================================================


async def _run_export_with_content(db_conn, scheme_id: str, content: str, config: dict | None = None):
    from app.routers.export import export_docx

    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        (scheme_id, f"p-{scheme_id}", "R221 图表归因"),
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (f"c-{scheme_id}", scheme_id, "", 0, 1, "第一章 施工", "generated", content),
    )
    await db_conn.commit()
    resp = await export_docx(
        scheme_id,
        {
            "project_name": "示例项目",
            "bid_section": "第一标段",
            "config": config or {},
            "chart_images": [],
            "fe_codes": [],
            "override_high_issues": True,
        },
        db=db_conn,
    )
    assert isinstance(resp, FileResponse), resp
    return json.loads(resp.headers.get("X-Chart-Render-Stats") or "{}")


@pytest.mark.asyncio
async def test_architecture_syntax_failure_exposes_no_pil_fallback(db_conn, monkeypatch):
    """R221 P1：类型在 7 类白名单内、但语法载荷无 PIL 解析器 → 必须报专用告警。"""
    from app.services.ai import mermaid_renderer as mr

    monkeypatch.setattr(mr._chart_cache, "get_or_render", lambda *_a, **_k: None)
    stats = await _run_export_with_content(
        db_conn,
        "s-r221-arch",
        "组织架构如下。\n\n```mermaid\narchitecture-beta\n  service A\n  service B\n```\n",
    )
    assert stats.get("failed") == 1, stats
    assert stats.get("no_pil_fallback") == 1, stats
    assert stats.get("no_pil_fallback_types") == ["architecture"], stats
    assert not stats.get("no_http_fallback"), stats


@pytest.mark.asyncio
async def test_non_gantt_json_failure_exposes_no_http_fallback(db_conn, monkeypatch):
    """R221 P2：7 类 chart-json 都没有 HTTP 出口，不能只认甘特。"""
    from app.services.ai import mermaid_renderer as mr

    monkeypatch.setattr(mr._chart_cache, "get_or_render", lambda *_a, **_k: None)
    stats = await _run_export_with_content(
        db_conn,
        "s-r221-layout",
        "平面布置如下。\n\n```chart-json\n"
        '{"type":"layout","title":"总平面","zones":[{"name":"A区"},{"name":"B区"}]}\n'
        "```\n",
    )
    assert stats.get("failed") == 1, stats
    assert stats.get("no_http_fallback") == 1, stats
    assert stats.get("no_http_fallback_types") == ["layout"], stats
    assert not stats.get("no_pil_fallback"), stats


@pytest.mark.asyncio
async def test_disabled_pil_switch_gets_actionable_signal(db_conn, monkeypatch):
    """R221：开关关闭导致本可兜底的语法载荷丢图 → 单独成类，不与「无兜底」混淆。"""
    from app.services.ai import mermaid_renderer as mr

    monkeypatch.setattr(mr._chart_cache, "get_or_render", lambda *_a, **_k: None)
    stats = await _run_export_with_content(
        db_conn,
        "s-r221-switch",
        "工艺流程如下。\n\n```mermaid\nflowchart TD\n  A[准备] --> B[施工]\n```\n",
        config={"allow_pil_fallback": False},
    )
    assert stats.get("failed") == 1, stats
    assert stats.get("pil_fallback_disabled") == 1, stats
    assert stats.get("pil_fallback_disabled_types") == ["flowchart"], stats
    assert not stats.get("no_pil_fallback"), stats


@pytest.mark.asyncio
async def test_successful_pil_fallback_is_counted(db_conn, monkeypatch):
    """报告 §6.1 ②：本次导出有多少张图实际走了 PIL 兜底，必须可见。"""
    from app.services.ai import mermaid_renderer as mr

    def _fake_render(code, ct, fmt, skip_http, allow_pil, strategy_out=None):
        if isinstance(strategy_out, dict):
            strategy_out["strategy"] = "pil_syntax_fallback"
        return BytesIO(_real_png())

    monkeypatch.setattr(mr._chart_cache, "get_or_render", _fake_render)
    stats = await _run_export_with_content(
        db_conn,
        "s-r221-pil",
        "进度计划如下。\n\n```mermaid\ngantt\n title 进度\n dateFormat YYYY-MM-DD\n"
        " 施工准备 :2026-01-01, 5d\n```\n",
    )
    assert stats.get("backend_ok") == 1, stats
    assert stats.get("pil_fallback") == 1, stats
    assert not stats.get("failed"), stats
