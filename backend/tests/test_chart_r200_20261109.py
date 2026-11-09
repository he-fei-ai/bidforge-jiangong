# -*- coding: utf-8 -*-
"""R200 图表模块遗留项收口护栏（2026-11-09）。

承接 R199 遗留清单，按最优方案执行修复/加固并锁定：
  A. 单次图像探测 ``_chart_probe``（格式白名单 + verify + 尺寸 + DPI 合并一次
     PIL 打开；happy path 插入侧不再二次探测）；
  B. fix-mermaid ``fence_ordinal`` 围栏序号精确修复（前后端 + 契约类型棘轮）；
  C. G1 跨端 parity：后端删图/降级枚举 ↔ 前端中文文案封闭映射；
  D. G3 apply 失败/INSERT 失败行为实证（G3 端到端缺口收口）；
  E. ``sections.inlined_chart_json`` 死列棘轮（唯一出现点，新代码禁读写）；
  F. export 侧 probe 接线锁。

⚠️ 只跑本文件：
    python -m pytest tests/test_chart_r200_20261109.py -q
"""

from __future__ import annotations  # noqa: I001  — 块级：export 必须先于 _export_docx_parts / _chart_pipeline（循环引用顺序敏感，R195 教训）

import asyncio
import io
from pathlib import Path

import pytest
from PIL import Image

import app.routers.export  # noqa: F401  — 循环引用顺序敏感：export 必须先于 _export_docx_parts（R195 教训）
import app.routers._chart_pipeline as CP
from app.routers._chart_pipeline import apply_inline_chart_plan, build_inline_chart_plan
from app.routers._export_docx_parts import (
    _chart_ok,
    _chart_probe,
    _image_format_supported,
    _insert_image_with_caption,
)

APP = Path(__file__).resolve().parents[1] / "app"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _png(w: int = 64, h: int = 48, dpi=None) -> bytes:
    buf = io.BytesIO()
    kw = {"dpi": dpi} if dpi else {}
    Image.new("RGB", (w, h), (10, 120, 30)).save(buf, "PNG", **kw)
    return buf.getvalue()


# ============================================================
# A. 单次图像探测 _chart_probe
# ============================================================


class TestProbeSingleDecode:
    def test_probe_returns_metadata(self):
        p = _chart_probe(io.BytesIO(_png(80, 60, dpi=(96, 96))))
        assert p is not None
        assert p["w"] == 80 and p["h"] == 60
        assert p["dpi"] >= 0

    def test_probe_rejects_bad_bytes(self):
        assert _chart_probe(None) is None
        assert _chart_probe(io.BytesIO(b"x" * 50)) is None  # 过小
        assert _chart_probe(io.BytesIO(b"X" * 500)) is None  # 垃圾字节
        good = _png()
        assert _chart_probe(io.BytesIO(good[: len(good) // 2])) is None  # 截断（R199 不变式保持）

    def test_wrappers_equivalent(self):
        assert _chart_ok(io.BytesIO(_png())) is True
        assert _image_format_supported(io.BytesIO(_png())) is True
        assert _chart_ok(io.BytesIO(b"y" * 500)) is False

    def test_insert_with_probe_skips_pil_open(self, monkeypatch):
        """probe 提供时插入侧不得再打开 PIL（单次探测的核心收益）。"""
        from docx import Document

        def _boom(*_a, **_k):
            raise RuntimeError("probe 已提供，插入侧禁止再打开 PIL")

        monkeypatch.setattr("PIL.Image.open", _boom)
        doc = Document()
        ok = _insert_image_with_caption(
            doc, io.BytesIO(_png()), "图 1-1 测试", probe={"w": 64, "h": 48, "dpi": 96.0}
        )
        assert ok is True

    def test_insert_without_probe_still_probes(self, monkeypatch):
        """probe 缺省（兼容路径）保持旧行为：插入侧自行探测。"""
        from docx import Document

        calls = []
        real_open = Image.open

        def _spy(*a, **k):
            calls.append(1)
            return real_open(*a, **k)

        monkeypatch.setattr("PIL.Image.open", _spy)
        doc = Document()
        assert _insert_image_with_caption(doc, io.BytesIO(_png()), "图 1-1 测试") is True
        assert calls, "无 probe 时必须走自探测路径"

    def test_export_branches_wired(self):
        """export.py 图表/AI 配图两分支都必须接 _chart_probe 并透传 probe。"""
        src = (APP / "routers" / "export.py").read_text(encoding="utf-8")
        assert src.count("_chart_probe(") >= 2, "图表与 AI 配图分支各一处单次探测"
        assert "probe=_probe," in src and "probe=_img_probe," in src


# ============================================================
# B. fix-mermaid fence_ordinal 精确修复
# ============================================================


class TestFixMermaidOrdinal:
    def test_backend_selection_wired(self):
        src = (APP / "routers" / "charts.py").read_text(encoding="utf-8")
        assert "fence_ordinal" in src
        assert "_chosen_ord" in src and "_apply_chart_fence_edits(old_content, {_chosen_ord: fixed})" in src
        # 序号与代码双重吻合才采纳；不吻合告警回退首块
        assert "与正文同码块" in src and "回退首块" in src

    def test_frontend_wired(self):
        md = (FRONTEND / "components" / "MarkdownRenderer.tsx").read_text(encoding="utf-8")
        assert "data-fix-ordinal" in md, "修复按钮必须携带围栏序号"
        assert "fence_ordinal" in md, "修复请求必须透传 fence_ordinal"
        api = (FRONTEND / "api" / "index.ts").read_text(encoding="utf-8")
        assert "fence_ordinal?: number" in api, "契约类型必须登记 fence_ordinal"

    def test_apply_chosen_ord_semantics(self):
        """行为锚：_apply_chart_fence_edits 按序号只改目标块（序号语义与前端一致）。"""
        from app.routers._chart_pipeline import _apply_chart_fence_edits, _scan_chart_fences_full

        content = "```mermaid\nflowchart TD\n    A --> B\n```\n正文\n```mermaid\nflowchart TD\n    A --> B\n```"
        ords = [_o for _ct, _cd, _o in _scan_chart_fences_full(content) if "flowchart TD" in _cd]
        assert ords == [0, 1]
        patched = _apply_chart_fence_edits(content, {1: "flowchart TD\n    X --> Y"})
        assert patched.count("X --> Y") == 1 and patched.count("A --> B") == 1


# ============================================================
# C. G1 跨端 parity：删图/降级枚举 ↔ 前端中文文案
# ============================================================

#: 后端全部 drop reason（build 侧 8 + R196 min_words + R199 降级 2）
BACKEND_DROP_REASONS = {
    "per_section_limit",
    "scheme_type_limit",
    "validation_failed",
    "missing_prompt",
    "empty_envelope",
    "invalid_json",
    "insert_failed",
    "insert_none",
    "min_words",
    "apply_failed",
    "register_cleaned",
}


class TestCrossEndParity:
    def test_every_backend_reason_has_frontend_label(self):
        fe = (FRONTEND / "utils" / "contentEvents.ts").read_text(encoding="utf-8")
        missing = [r for r in sorted(BACKEND_DROP_REASONS) if f"{r}:" not in fe]
        assert not missing, f"前端 CHART_DROP_REASON_LABELS 缺少中文文案: {missing}"

    def test_register_channel_and_summary_charts(self):
        fe = (FRONTEND / "utils" / "contentEvents.ts").read_text(encoding="utf-8")
        assert "图表登记" in fe, "降级信号 type=register 必须有可读中文通道名"
        assert "charts:" in fe, "contentResultSummary 必须透出 charts（断线重挂「配图 N 张」）"

    def test_frontend_labels_are_chinese(self):
        fe = (FRONTEND / "utils" / "contentEvents.ts").read_text(encoding="utf-8")
        for r in sorted(BACKEND_DROP_REASONS):
            i = fe.index(f"{r}:")
            val = fe[i + len(r) + 1 : fe.index("\n", i)]
            assert any("\u4e00" <= ch <= "\u9fff" for ch in val), f"{r} 的文案必须为中文"


# ============================================================
# D. G3 apply 失败 / INSERT 失败行为实证
# ============================================================


class _FakeDb:
    """最小异步 db 桩：可按 SQL 关键词注入失败。"""

    def __init__(self, fail_on: str = ""):
        self.fail_on = fail_on

    async def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise ValueError(f"deterministic boom: {self.fail_on}")

        class _Cur:
            rowcount = 1

            async def fetchall(self):
                return []

        return _Cur()


def _one_chart_row(monkeypatch):
    """构造一条合法 flowchart 登记行（绕过 min_words 闸；≥3 节点过生产校验）。"""
    monkeypatch.setattr(CP, "_CHART_MIN_WORDS", 0)
    content = "```mermaid\nflowchart TD\n    A --> B\n    B --> C\n```"
    _new, rows = build_inline_chart_plan("sch-g3", "sec-g3", content, enforce_limits=True)
    assert len(rows) == 1
    return rows


class TestApplyFailureBehavior:
    def test_delete_failure_propagates(self, monkeypatch):
        """DELETE 整体失败（retry 预算耗尽）必须向上抛 —— 由 sse_handlers 捕获
        后记 register/apply_failed，正文照常落库（正文是主产物）。"""
        rows = _one_chart_row(monkeypatch)
        with pytest.raises(ValueError):
            asyncio.run(
                apply_inline_chart_plan(_FakeDb(fail_on="DELETE FROM chart_predictions"), "sec-g3", rows)
            )

    def test_insert_failure_degrades_and_clips(self, monkeypatch):
        """单行 INSERT 失败降级为 insert_failed 跳过登记 + 正文同步裁剪（不抛）。"""
        rows = _one_chart_row(monkeypatch)
        content = "```mermaid\nflowchart TD\n    A --> B\n    B --> C\n```"
        dropped = []
        out = asyncio.run(
            apply_inline_chart_plan(
                _FakeDb(fail_on="INSERT INTO chart_predictions"), "sec-g3", rows, content, dropped
            )
        )
        assert ("flowchart", "insert_failed") in [(d["type"], d["reason"]) for d in dropped]
        assert out is not None and "```mermaid" not in out, "登记失败的块必须同步从正文移除（防幽灵图）"

    def test_success_reports_inserted_count(self, monkeypatch):
        rows = _one_chart_row(monkeypatch)
        inserted = []
        out = asyncio.run(
            apply_inline_chart_plan(
                _FakeDb(),
                "sec-g3",
                rows,
                "```mermaid\nflowchart TD\n    A --> B\n    B --> C\n```",
                inserted_out=inserted,
            )
        )
        assert inserted == [1]
        assert out is not None

    def test_sse_degradation_signals_locked(self):
        """sse_handlers 两个降级出口必须补 charts_dropped 信号（R199 FIX-5）。"""
        src = (APP / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert '{"type": "register", "reason": "apply_failed"}' in src
        assert '{"type": "register", "reason": "register_cleaned"}' in src


# ============================================================
# E. sections.inlined_chart_json 死列棘轮
# ============================================================


class TestDeadColumnRatchet:
    def test_only_known_sites(self):
        hits = {}
        for py in APP.rglob("*.py"):
            t = py.read_text(encoding="utf-8", errors="replace")
            if "inlined_chart_json" in t:
                hits[py.relative_to(APP).as_posix()] = t
        assert set(hits) <= {"schema_sql.py", "db.py", "routers/sections.py"}, f"死列出现新读写点: {sorted(hits)}"
        # 唯一运行时出现点是方案删除清理（置空），非业务读写
        assert "status='empty'" in hits["routers/sections.py"]
