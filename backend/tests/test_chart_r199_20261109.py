# -*- coding: utf-8 -*-
"""R199（2026-11-09 · 图表生成与渲染模块深度审计收口）护栏测试。

六组：A apply enforce_limits 门控 + 实写数出参（行为级）；B 坏图拒收
（行为级）；C ChartCache 磁盘坏缓存自愈 + .tmp 过滤（行为级）；D HTTP
后端全不可用短路（行为级 + 单元）；E 登记降级信号（源码锁）；F 杂项
源码锁 + 字节体检参数化。
"""

from __future__ import annotations  # noqa: I001  — 块级：下方 export 先于 _chart_pipeline 的导入顺序刻意（R195 教训）

import asyncio  # noqa: I001  — 下两行 app 导入顺序刻意（export 必须先于 _export_docx_parts，R195 教训）
import io
from pathlib import Path

import pytest

import app.routers.export  # noqa: F401,E402  ✅ R195 教训：export↔_export_docx_parts 循环引用导入顺序敏感，须先 import export
import app.routers._chart_pipeline as CP  # noqa: E402
from app.routers._chart_pipeline import (
    _CHART_SCHEME_TYPE_DEFAULT_LIMIT,
    apply_inline_chart_plan,
    resync_inline_charts,
)

APP = Path(CP.__file__).resolve().parents[1]  # backend/app
BACKEND = APP.parent  # backend/
REPO = BACKEND.parent
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_VALID_FLOWCHART = "flowchart TD\n    A[开始] --> B[处理]\n    B --> C[结束]"


def _make_png(w: int = 320, h: int = 240) -> bytes:
    """生成尺寸足够大的 PNG（噪声像素），截断后仍 >100 字节可区分新旧判据。"""
    import random

    from PIL import Image

    rnd = random.Random(199)
    img = Image.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(300, 300))
    return buf.getvalue()


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# =========================================================================
# A · apply_inline_chart_plan：enforce_limits 门控 + inserted_out 出参
# =========================================================================


class _Cur:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    async def fetchall(self):
        return self._rows


class _FakeDb:
    """最小 aiosqlite 替身：记录执行语句；限额复核 SELECT 返回预设计数。"""

    def __init__(self, live_counts=None):
        self.live_counts = live_counts or {}
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = sql.strip()
        if s.startswith("SELECT chart_type, COUNT(*)"):
            return _Cur([{"chart_type": k, "n": v} for k, v in self.live_counts.items()])
        return _Cur()


def _row(section_id="sec-1", scheme_id="sch-1", ct="flowchart"):
    import uuid

    payload = '{"mermaid_code":"' + _VALID_FLOWCHART.replace("\n", "\\n") + '","title":"t"}'
    return (str(uuid.uuid4()), section_id, scheme_id, ct, "正文同步生成", 5, "generated", payload)


_CONTENT = f"正文段落。\n\n```mermaid\n{_VALID_FLOWCHART}\n```\n\n结尾段落。"


class TestApplyEnforceGate:
    def test_enforce_false_skips_limit_review(self):
        """R199 主修复：enforce_limits=False 时事务内限额复核不执行 ——
        手动保存路径用户显式保存的图不被静默删块（契约 sections.py:1004）。"""
        db = _FakeDb(live_counts={"flowchart": _CHART_SCHEME_TYPE_DEFAULT_LIMIT})
        rows = [_row()]
        out: list = []
        content = run(apply_inline_chart_plan(db, "sec-1", rows, _CONTENT, dropped_out=out, enforce_limits=False))
        assert content == _CONTENT, "enforce_limits=False 不得裁剪正文"
        assert not any(o.get("reason") == "scheme_type_limit" for o in out)
        assert any(s.strip().startswith("INSERT INTO chart_predictions") for s, _ in db.executed)
        # 限额 SELECT 也不应发起（省一次查询）
        assert not any(s.strip().startswith("SELECT chart_type, COUNT(*)") for s, _ in db.executed)

    def test_enforce_true_default_still_trims(self):
        """默认 True 保持生成链路行为不变：满额类型跳过登记并同步删块。"""
        db = _FakeDb(live_counts={"flowchart": _CHART_SCHEME_TYPE_DEFAULT_LIMIT})
        out: list = []
        content = run(apply_inline_chart_plan(db, "sec-1", [_row()], _CONTENT, dropped_out=out))
        assert any(o == {"type": "flowchart", "reason": "scheme_type_limit"} for o in out)
        assert "```mermaid" not in content
        assert not any(s.strip().startswith("INSERT INTO chart_predictions") for s, _ in db.executed)

    def test_inserted_out_reports_real_count(self):
        db = _FakeDb()
        inserted: list[int] = []
        run(apply_inline_chart_plan(db, "sec-1", [_row(), _row()], None, inserted_out=inserted))
        assert inserted == [2]

    def test_inserted_out_counts_only_written(self):
        """同类型超限被复核跳过的行不计入实写数。"""
        db = _FakeDb()
        limit = _CHART_SCHEME_TYPE_DEFAULT_LIMIT
        inserted: list[int] = []
        run(apply_inline_chart_plan(db, "sec-1", [_row() for _ in range(limit + 2)], None, inserted_out=inserted))
        assert inserted == [limit]

    def test_resync_returns_real_count(self):
        """resync_inline_charts 返回实写数（旧实现按 len(rows) 虚高）。"""
        db = _FakeDb()
        n, content = run(resync_inline_charts(db, "sch-1", "sec-1", _CONTENT))
        assert n == 1
        assert "```mermaid" in content

    def test_resync_enforce_false_passes_through(self):
        """resync 的 enforce_limits=False 透传到 apply（手动保存链契约）。"""
        db = _FakeDb(live_counts={"flowchart": _CHART_SCHEME_TYPE_DEFAULT_LIMIT})
        n, content = run(resync_inline_charts(db, "sch-1", "sec-1", _CONTENT, enforce_limits=False))
        assert n == 1
        assert content == _CONTENT
        assert any(s.strip().startswith("INSERT INTO chart_predictions") for s, _ in db.executed)


# =========================================================================
# B · 坏图拒收：_chart_ok / _cached_bytes_ok 全文件校验
# =========================================================================


class TestBadImageRejection:
    def test_chart_ok_rejects_truncated_png(self):
        from app.routers._export_docx_parts import _chart_ok

        png = _make_png()
        assert _chart_ok(io.BytesIO(png)) is True
        # 截断：砍掉后 40%（头部完好，旧 format 判定可过；verify 必须拒收）
        truncated = io.BytesIO(png[: int(len(png) * 0.6)])
        assert len(truncated.getvalue()) > 100, "截断样本必须仍 >100 字节（否则旧判据也会拒）"
        assert _chart_ok(truncated) is False

    def test_chart_ok_rejects_garbage_over_100_bytes(self):
        from app.routers._export_docx_parts import _chart_ok

        assert _chart_ok(io.BytesIO(b"X" * 500)) is False

    def test_cached_bytes_ok_mirrors_chart_ok(self):
        from app.services.ai.mermaid_renderer import _cached_bytes_ok

        png = _make_png()
        assert _cached_bytes_ok(png) is True
        assert _cached_bytes_ok(png[: int(len(png) * 0.6)]) is False
        assert _cached_bytes_ok(b"") is False
        assert _cached_bytes_ok(b"Y" * 500) is False

    def test_image_format_supported_rejects_truncated(self):
        from app.routers._export_docx_parts import _image_format_supported

        png = _make_png()
        assert _image_format_supported(io.BytesIO(png)) is True
        assert _image_format_supported(io.BytesIO(png[: int(len(png) * 0.6)])) is False


# =========================================================================
# C · ChartCache：磁盘坏缓存自愈 + .tmp 过滤
# =========================================================================


class TestChartCacheSelfHeal:
    @pytest.fixture()
    def cache(self, tmp_path, monkeypatch):
        from app.services.ai import mermaid_renderer as MR

        c = MR.ChartCache(cache_dir=tmp_path)
        monkeypatch.setattr(
            MR,
            "render_mermaid_to_bytes",
            lambda code, ct, skip_http=False, allow_pil=None: io.BytesIO(_make_png()),
        )
        return c, MR

    def test_corrupt_disk_cache_is_deleted_and_rerendered(self, cache):
        """坏缓存：命中即删 + 重渲自愈 —— 不再永久固化坏字节。"""
        c, MR = cache
        key = c._cache_key("gantt", "gantt\nA", True, False)
        bad = c.cache_dir / f"{key}.png"
        bad.write_bytes(PNG_MAGIC + b"truncated-garbage" * 10)  # 头部像 PNG、体损坏
        out = c.get_or_render("gantt\nA", "gantt", allow_pil=True, skip_http=False)
        assert out is not None and out.getvalue() == _make_png()
        assert bad.read_bytes() == _make_png(), "坏文件必须被有效渲染结果覆盖"

    def test_follower_corrupt_cache_branch_source_lock(self):
        """follower 命中坏缓存分支：不按 leader 失败返回 None（源码锁）。"""
        src = (APP / "services" / "ai" / "mermaid_renderer.py").read_text(encoding="utf-8")
        assert "_corrupt_cache = False" in src
        assert "follower 命中坏缓存" in src
        assert "not _signaled or _corrupt_cache" in src

    def test_valid_cache_still_hits(self, cache):
        c, MR = cache
        c.set("gantt\nC", _make_png(), chart_type="gantt", allow_pil=True, skip_http=False)

        def _boom(*a, **k):
            raise AssertionError("有效缓存命中不得重渲")

        MR.render_mermaid_to_bytes = _boom
        out = c.get_or_render("gantt\nC", "gantt", allow_pil=True, skip_http=False)
        assert out.getvalue() == _make_png()

    def test_tmp_files_excluded_from_disk_state(self, cache):
        """进程强杀残留的 .tmp 孤儿不计入 LRU 与 total_size。"""
        c, _ = cache
        (c.cache_dir / "abc.123.tmp").write_bytes(b"z" * 1000)
        png = _make_png()
        (c.cache_dir / "real.png").write_bytes(png)
        c._sync_state_from_disk()
        assert c._stats["total_size"] == len(png), ".tmp 残留被计入配额"
        assert not any(k.startswith("abc") for k in c._access_order)


# =========================================================================
# D · HTTP 渲染后端全不可用短路
# =========================================================================


class TestHttpShortCircuit:
    def test_service_all_backends_probed_unavailable(self):
        import time as _time

        from app.services.ai.mermaid_service import MermaidRenderBackend, MermaidServiceClient

        cli = MermaidServiceClient()
        assert cli.all_backends_probed_unavailable() is False, "未探测过必须保守返回 False"
        now = _time.time()
        for b in MermaidRenderBackend:
            cli._backend_available[b] = False
            cli._backend_cache_time[b] = now
        assert cli.all_backends_probed_unavailable() is True
        # 任一后端可用 → False
        cli._backend_available[MermaidRenderBackend.DOCKER] = True
        assert cli.all_backends_probed_unavailable() is False
        # 缓存过期 → 保守 False
        cli._backend_available[MermaidRenderBackend.DOCKER] = False
        cli._backend_cache_time[MermaidRenderBackend.DOCKER] = now - 10_000
        assert cli.all_backends_probed_unavailable() is False

    def test_renderer_skips_http_when_all_unavailable(self, monkeypatch):
        """探测缓存全不可用 → 不进入 HTTP 重试循环（旧实现每图空转 2 次 + sleep）。"""
        from app.services.ai import mermaid_renderer as MR

        class _FakeClient:
            def all_backends_probed_unavailable(self):
                return True

        monkeypatch.setattr(MR, "get_mermaid_service_client", lambda timeout=30: _FakeClient())
        out = MR.render_mermaid_to_bytes("flowchart TD\nA-->B", "flowchart", skip_http=False, allow_pil=False)
        assert out is None  # allow_pil=False（不降级）→ 直接失败语义，未发起任何 HTTP

    def test_renderer_http_attempted_when_probed_available(self, monkeypatch):
        """探测缓存显示可用（或未探测）→ 保持原行为发起 HTTP。"""
        from app.services.ai import mermaid_renderer as MR

        attempts = {"n": 0}

        class _FakeClient:
            def all_backends_probed_unavailable(self):
                return False

            async def render_mermaid_via_http(self, **kw):
                attempts["n"] += 1
                raise RuntimeError("boom")

        monkeypatch.setattr(MR, "get_mermaid_service_client", lambda timeout=30: _FakeClient())
        out = MR.render_mermaid_to_bytes("flowchart TD\nA-->B", "flowchart", skip_http=False, allow_pil=False)
        assert attempts["n"] >= 1, "探测可用时必须照常尝试 HTTP"
        assert out is None


# =========================================================================
# E · 登记降级信号（R199）：apply_failed / register_cleaned / charts 摘要
# =========================================================================


def _sh_src():
    return (APP / "routers" / "sse_handlers.py").read_text(encoding="utf-8")


class TestDegradedSignal:
    def test_apply_failed_outlet_appends_register_drop(self):
        src = _sh_src()
        assert '"reason": "apply_failed"' in src, "apply 失败降级出口必须追加 register/apply_failed 信号"
        i = src.index('"reason": "apply_failed"')
        seg = src[max(0, i - 400) : i + 400]
        assert 'report["charts_dropped"] = _chart_dropped' in seg
        assert 'report["charts_dropped_count"] = len(_chart_dropped)' in seg

    def test_build_failed_outlet_appends_register_cleaned(self):
        src = _sh_src()
        assert '"reason": "register_cleaned"' in src, "build 失败降级出口必须补 register_cleaned 信号"

    def test_checkpoint_payload_carries_charts(self):
        src = (APP / "routers" / "_sse_content_payloads.py").read_text(encoding="utf-8")
        assert '"charts": int(ckpt.get("charts", 0) or 0)' in src
        assert src.count("over_count") >= 2  # 唯一拼装点结构未破坏

    def test_ckpt_charts_accumulated_on_section_done(self):
        src = _sh_src()
        assert '_ckpt["charts"] = int(_ckpt.get("charts", 0) or 0)' in src
        i = src.index('_ckpt["charts"]')
        seg = src[max(0, i - 1000) : i]
        assert "if count_progress:" in seg, "配图累加必须在 count_progress 门控内（防字数批调度双计）"

    def test_frontend_labels_registered(self):
        fe = REPO / "frontend" / "src" / "utils" / "contentEvents.ts"
        src = fe.read_text(encoding="utf-8")
        for reason in ("apply_failed", "register_cleaned"):
            assert f"{reason}:" in src, f"前端缺 {reason} 中文文案"


# =========================================================================
# F · 杂项源码锁 + 字节体检
# =========================================================================


class TestSourceLocks:
    def test_min_words_hoisted_out_of_loop(self):
        """R199：实际字数计算外提（旧实现在图表块循环体内重复全文计数）。"""
        src = (APP / "routers" / "_chart_pipeline.py").read_text(encoding="utf-8")
        i_loop = src.index("for ct, code, ordinal in charts:")
        body = src[:i_loop]
        assert body.count("_actual_words = text_word_count(content)") == 1
        after = src[i_loop : src.index("async def apply_inline_chart_plan(")]
        assert "_actual_words = text_word_count(content)" not in after

    def test_resync_returns_inserted_count(self):
        src = (APP / "routers" / "_chart_pipeline.py").read_text(encoding="utf-8")
        assert "_inserted[0] if _inserted else len(rows)" in src

    def test_db_zombie_migration_comment_fixed(self):
        src = (APP / "db.py").read_text(encoding="utf-8")
        assert "没有补生成消费者" in src
        i = src.index("重置为 pending 让 Phase 3 补生成")
        seg = src[max(0, i - 500) : i]
        assert "2026-09-13 整体删除" in seg

    def test_fix_mermaid_multi_match_warning(self):
        """✅ R200 重锚：多块同码从「只改第 1 块」升级为「按前端围栏序号精确改写，
        序号不吻合告警回退首块」—— 多匹配 WARNING 留痕语义保留。"""
        src = (APP / "routers" / "charts.py").read_text(encoding="utf-8")
        i = src.index("_target_ords = [")
        seg = src[i : i + 2400]
        assert "len(_target_ords) > 1" in seg and "改写序号" in seg
        assert "_chosen_ord" in seg and "fence_ordinal" in seg

    def test_dead_config_annotated(self):
        src = (APP / "config.py").read_text(encoding="utf-8")
        i = src.index("image_max_per_project: int = 30")
        seg = src[max(0, i - 500) : i]
        assert "死配置" in seg, "image_max_per_project 必须标注死配置防误导"

    def test_mermaid_service_bg_tasks_hold_reference(self):
        src = (APP / "services" / "ai" / "mermaid_service.py").read_text(encoding="utf-8")
        assert "self._bg_tasks.add(_t)" in src
        assert "add_done_callback(self._bg_tasks.discard)" in src

    def test_export_docx_parts_pil_image_closed(self):
        """✅ R200 重锚：探测上收 _chart_probe（with-block + verify，探测流独立于
        add_picture 流）；_insert 兼容路径仍保留独立 probe 流 + finally close。"""
        src = (APP / "routers" / "_export_docx_parts.py").read_text(encoding="utf-8")
        i = src.index("def _chart_probe")
        seg = src[i : i + 1600]
        assert 'PILImage.open(_sh.BytesIO(img_bytes.getvalue())) as im' in seg
        assert "im.verify()" in seg
        j = src.index("_probe_io = _sh.BytesIO(img_bytes.getvalue())")
        seg2 = src[j : j + 400]
        assert "_im = PILImage.open(_probe_io)" in seg2
        assert "finally:" in seg2 and "_im.close()" in seg2
        # 探测实现（_chart_probe）位于插入实现之前；插入流独立于探测流
        assert i < src.index("def _insert_image_with_caption")

    def test_image_format_supported_has_verify(self):
        src = (APP / "routers" / "_export_docx_parts.py").read_text(encoding="utf-8")
        i = src.index("def _image_format_supported")
        seg = src[i : i + 1800]
        assert "im.verify()" in seg

    def test_apply_signature_has_new_kwargs(self):
        src = (APP / "routers" / "_chart_pipeline.py").read_text(encoding="utf-8")
        i = src.index("async def apply_inline_chart_plan(")
        sig = src[i : src.index(") -> str | None:", i)]
        assert "enforce_limits: bool = True" in sig
        assert "inserted_out: list[int] | None = None" in sig

    def test_renderer_shortcut_before_retry_loop(self):
        """短路必须发生在重试循环之前（否则空转照旧）。"""
        src = (APP / "services" / "ai" / "mermaid_renderer.py").read_text(encoding="utf-8")
        i_short = src.index("all_backends_probed_unavailable()")
        i_loop = src.index("for _retry in range(_max_retries")
        assert i_short < i_loop


class TestByteIntegrity:
    @pytest.mark.parametrize(
        "rel,expect",
        [
            ("app/routers/_chart_pipeline.py", "CRLF"),
            ("app/routers/sse_handlers.py", "CRLF"),
            ("app/routers/_sse_content_payloads.py", "CRLF"),
            ("app/routers/_export_docx_parts.py", "CRLF"),
            ("app/routers/charts.py", "CRLF"),
            ("app/db.py", "CRLF"),
            ("app/config.py", "CRLF"),
            ("app/services/ai/mermaid_renderer.py", "CRLF"),
            ("app/services/ai/mermaid_service.py", "CRLF"),
            ("../frontend/src/utils/contentEvents.ts", "CRLF"),
            ("../frontend/src/tests/contentEvents.test.ts", "CRLF"),
            ("../frontend/src/pages/SchemeWorkbenchPage.tsx", "LF"),
            ("../frontend/src/hooks/useContentGeneration.ts", "CRLF"),
            ("../frontend/src/components/MarkdownRenderer.tsx", "CRLF"),
        ],
    )
    def test_line_endings_preserved(self, rel, expect):
        fp = (BACKEND / rel).resolve()
        b = fp.read_bytes()
        crlf = b.count(b"\r\n")
        lf = b.count(b"\n")
        assert b.count(b"\xef\xbf\xbd") == 0, f"FFFD 污染: {fp.name}"
        if expect == "CRLF":
            assert crlf == lf, f"{fp.name} 应为纯 CRLF（crlf={crlf}/lf={lf}）"
        else:
            assert crlf == 0, f"{fp.name} 应为纯 LF（crlf={crlf}）"
