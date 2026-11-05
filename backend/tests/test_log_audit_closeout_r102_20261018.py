# -*- coding: utf-8 -*-
"""R102 日志深度审计收口护栏（2026-10-18）。

背景（日志采集与基线建立）：对 backend/logs/ 2026-10-01 快照
（backend_err.log 4173 行 + backend_out.log 1953 行，GBK 控制台重定向）
做全量解析，基线：1952 请求 / 错误率 0.92%（18 条 4xx5xx）/
p50=12ms · p95=543ms · p99=17.4s / 107 条 provider_factory ERROR
（429×79 + UnsupportedModel 404×18）/ 837 条 WARNING。
本轮收口三项：

A 组 · 图表渲染截断载荷（P1）：生产实证 trace=646c14650b7d —— AI 生成的
   342 字符 gantt JSON 被 max_tokens 截断 → `_infer_payload_type` 的
   json.loads 失败返回空串 → 前端默认 chart_type=flowchart 拿到 gantt 数据
   → PIL v2 必然失败 → 500 且文案误导（"请检查 Mermaid 代码语法"）。
   修复：`/charts/render` 对「以 { 开头但非法 JSON」提前 400（可行动文案）；
   `mermaid_renderer` 对损坏载荷记明确 WARNING（不再只有"建议检查 HTTP 服务"）。

C 组 · uvicorn.access trace 埋点缺口（P1）：uvicorn 自带 handler 的
   `uvicorn`/`uvicorn.access` 为 propagate=false → 访问日志不过 root 的
   TraceContextFilter，访问行恒缺 [trace=…]（基线 613 条 /health 只以无
   trace 行存在）。修复：main.py 补挂 filter（幂等）。

D 组 · 控制台日志编码统一（P1）：重定向日志全 GBK（1412 行含中文），与
   logs/backend.log（UTF-8）双轨 → 工具链解析乱码。修复：非 TTY 时
   reconfigure stdout/stderr 为 UTF-8（TTY 保持 locale，控制台零回归）。
"""

import io
import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
CHARTS_PY = BACKEND / "app" / "routers" / "charts.py"
MAIN_PY = BACKEND / "app" / "main.py"
RENDERER_PY = BACKEND / "app" / "services" / "ai" / "mermaid_renderer.py"

# 复现 trace=646c14650b7d 的载荷形态：合法 JSON 外壳、tasks 中途截断
_TRUNCATED_GANTT = (
    '{"type":"gantt","title":"主要材料进场计划","fig_no":"",'
    '"tasks":[{"id":"t1","name":"瓷砖","start":"2026-03-01","end":"2026-03-15"},'
    '{"id":"t2","name":"钢筋","start":"2026-03-05","end":"2026-03-2'
)
_VALID_GANTT = json.dumps(
    {
        "type": "gantt",
        "title": "主要材料进场计划",
        "fig_no": "",
        "tasks": [{"id": "t1", "name": "瓷砖", "start": "2026-03-01", "end": "2026-03-15"}],
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------- A 组：截断载荷
class TestTruncatedJsonPayloadRejected:
    """/charts/render 对损坏的结构化载荷早退 400（替代误导性 500）。"""

    def test_truncated_json_returns_400_with_actionable_message(self):
        from app.main import app

        client = TestClient(app)
        r = client.post("/api/v1/charts/render", json={"chart_type": "flowchart", "code": _TRUNCATED_GANTT})
        assert r.status_code == 400, "截断的 JSON 载荷必须 400 早退（旧行为 500 且文案误导为 Mermaid 语法）"
        assert "重新生成" in r.text, f"400 文案必须可行动（引导重新生成）：{r.text[:200]}"

    def test_valid_json_payload_not_rejected_as_invalid(self):
        """合法 JSON 载荷不得被新守卫误伤（绝不因守卫返回 400）。"""
        from app.main import app

        client = TestClient(app)
        r = client.post("/api/v1/charts/render", json={"chart_type": "gantt", "code": _VALID_GANTT, "skip_http": True})
        assert r.status_code != 400 or "不是合法 JSON" not in r.text, "合法 JSON 载荷不得被误判为非法"

    def test_mermaid_syntax_payload_unaffected(self):
        """Mermaid 语法载荷不经 JSON 守卫（行为逐字兼容）。"""
        from app.main import app

        client = TestClient(app)
        code = "graph TD\n  A[开始] --> B[结束]"
        r = client.post("/api/v1/charts/render", json={"chart_type": "flowchart", "code": code, "skip_http": True})
        assert "不是合法 JSON" not in r.text

    def test_empty_code_still_400_missing_code(self):
        """空 code 的既有 400（缺少图表代码）保持不变。"""
        from app.main import app

        client = TestClient(app)
        r = client.post("/api/v1/charts/render", json={"chart_type": "flowchart", "code": ""})
        assert r.status_code == 400
        assert "缺少图表代码" in r.text


# ---------------------------------------------------------------- B 组：渲染器诊断
class TestRendererCorruptPayloadDiagnostics:
    """mermaid_renderer 对损坏 JSON 载荷记明确 WARNING（根因现场可见）。"""

    def test_corrupt_payload_logs_explicit_warning(self, caplog):
        from app.services.ai.mermaid_renderer import _chart_cache

        with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_renderer"):
            out = _chart_cache.get_or_render(_TRUNCATED_GANTT, "flowchart", "png", True)
        assert out is None, "截断载荷必然渲染失败（返回 None）"
        assert "JSON 数据载荷非法" in caplog.text, (
            "损坏载荷必须在渲染第一现场记明确 WARNING，" "而不是只剩'建议检查 HTTP 服务'的误导性建议"
        )

    def test_render_failure_advice_splits_json_vs_mermaid(self):
        """500 文案按载荷形态分流（源码锁）：JSON 载荷不再提示检查 Mermaid 语法。"""
        src = CHARTS_PY.read_text(encoding="utf-8")
        assert "_advice" in src and 'startswith("{")' in src, "charts.py 必须按载荷形态分流 500 建议文案"


# ---------------------------------------------------------------- C 组：uvicorn trace
class TestUvicornAccessTraceFilter:
    """uvicorn.access 访问日志必须携带 trace（闭合埋点缺口）。"""

    def test_main_installs_filter_on_uvicorn_loggers(self):
        import app.main  # noqa: F401 - import 即触发模块级挂载
        from app.utils.log_context import TraceContextFilter

        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            lg = logging.getLogger(name)
            has = any(isinstance(f, TraceContextFilter) for f in lg.filters) or any(
                isinstance(f, TraceContextFilter) for h in lg.handlers for f in h.filters
            )
            assert has, f"{name} 必须挂有 TraceContextFilter（R102）"

    def test_access_record_gets_trace_prefix(self):
        """功能实证：上下文有 trace 时，uvicorn.access 的记录带 [trace=…] 前缀。"""
        from app.utils.log_context import clear_context, set_context

        acc = logging.getLogger("uvicorn.access")
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(logging.Formatter("%(message)s"))
        acc.addHandler(h)
        # pytest 的 logging 插件会把 root 压到 WARNING → 显式提级再还原
        _old_level = acc.level
        acc.setLevel(logging.INFO)
        try:
            set_context(trace_id="r102probe01")
            acc.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/api/v1/health", "1.1", 200)
        finally:
            clear_context()
            acc.removeHandler(h)
            acc.setLevel(_old_level)
        assert "[trace=r102probe01]" in buf.getvalue(), "uvicorn.access 记录必须带 trace 前缀（grep 串链不断）"

    def test_install_filter_idempotent_on_reimport(self):
        """重复 import app.main 不重复挂 filter（幂等，无双重前缀）。"""
        import app.main  # noqa: F401
        from app.utils.log_context import TraceContextFilter

        acc = logging.getLogger("uvicorn.access")
        n_logger = sum(1 for f in acc.filters if isinstance(f, TraceContextFilter))
        n_handler = sum(1 for h in acc.handlers for f in h.filters if isinstance(f, TraceContextFilter))
        assert n_logger + n_handler == 1, f"filter 必须恰好一个（实测 {n_logger}+{n_handler}）"


# ---------------------------------------------------------------- D 组：编码与源码锁
class TestLoggingEncodingAndSourceLocks:
    """控制台流 UTF-8 化（非 TTY）+ 关键修复点的源码锁。"""

    def test_main_reconfigures_non_tty_streams_to_utf8(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "reconfigure" in src and 'encoding="utf-8"' in src, "main.py 必须对非 TTY 流 reconfigure 为 UTF-8"
        assert "isatty" in src, "必须按 TTY 判定，实时控制台保持 locale 零回归"

    def test_charts_guard_source_lock(self):
        """charts.py 守卫单一实现锁：json.loads 失败 → 400。"""
        src = CHARTS_PY.read_text(encoding="utf-8")
        assert "json.loads(mermaid_code)" in src
        assert "疑似内容生成时被截断而损坏" in src
        # 守卫必须位于渲染尝试之前（早退，不进渲染器白烧）
        i_guard = src.find("疑似内容生成时被截断而损坏")
        i_render = src.find("get_or_render")
        assert 0 < i_guard < i_render, "非法 JSON 守卫必须早于渲染调用"

    def test_renderer_warning_source_lock(self):
        src = RENDERER_PY.read_text(encoding="utf-8")
        assert "JSON 数据载荷非法" in src, "渲染器必须保留损坏载荷明确诊断"


# ---------------------------------------------------------------- E 组：字节体检
class TestByteHealth:
    """触碰文件字节体检：无 BOM、无孤立 CR、无 U+FFFD。"""

    @pytest.mark.parametrize("path", [CHARTS_PY, MAIN_PY, RENDERER_PY])
    def test_no_bom_no_orphan_cr_no_fffd(self, path: Path):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 不得带 UTF-8 BOM"
        assert b"\r" not in raw.replace(b"\r\n", b""), f"{path.name} 不得有孤立 CR"
        assert b"\xef\xbf\xbd" not in raw, f"{path.name} 不得含 U+FFFD 替换字符"
