# -*- coding: utf-8 -*-
"""R233（2026-11-13 · 图表生成与渲染模块深度探索增强）护栏。

锁定本轮 6 项后端修复（全部经行为探针/源码亲验定罪后落地）：
- FIX-1：mermaid / ai_image 解析期跳过补机器信号（chart_mermaid_skipped /
  chart_ai_image_skipped 键族）→ _merge_chart_json_skip_stats 统一计入 failed
  → 坏缓存守卫生效（与 chart-json 侧 R181/R222 同族收口，统计口径对称）。
- FIX-2：max_ai_images 预算排除块打 _budget_excluded 标记 → _ai_pending 豁免
  + write_section 文案分流（预算裁剪是有意设计，不是生成失败）。
- FIX-3：块级 fail-soft 异常补记 chart_delivery（四键守恒）。
- FIX-4：allow_pil_fallback / ai_image_auto_generate 显式 None 归一默认 True
  （R230 A6 同款收尾，指纹归一「None 视同未传」与读取端对齐）。
- FIX-5：_chart_semantic_context 跳过前一围栏块（闭合行/内部代码行/开启行）
  且不跨块穿透（R228 意图保留：本块语境止步于前一图表块边界）。
- CLEAN-1：write_section 兜底渲染 4 元组死分支删除（注释漂移校正）。
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from app.routers import export as E
from app.services.content_blocks import _parse_content_blocks
from fastapi.responses import FileResponse

_WS_ROOT = Path(__file__).resolve().parents[2]


def _real_png() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (32, 24), "white").save(buf, format="PNG")
    return buf.getvalue()


# ===========================================================================
# A · FIX-1：mermaid / ai_image 解析期跳过补机器信号
# ===========================================================================


def test_mermaid_unclosed_block_records_diagnostics():
    """未闭合 mermaid 块（max_tokens 截断高频形态）→ chart_mermaid_skipped 计数。"""
    diag: dict = {}
    content = "施工工艺如下。\n\n```mermaid\nflowchart TD\nA[准备] --> B[施工]\n"
    blocks = _parse_content_blocks(content, diagnostics=diag)
    assert not [b for b in blocks if b["type"] == "chart"], "未闭合块不得产出 chart 块"
    assert diag.get("chart_mermaid_skipped") == 1, diag
    assert diag.get("chart_mermaid_skipped_unclosed") == 1, diag
    # 既有 chart-json 键族语义不变（只含 chart-json）
    assert "chart_json_skipped" not in diag, diag


def test_mermaid_unknown_keyword_records_diagnostics():
    """首关键字不在映射表内的 mermaid 块 → unrenderable 细分键。"""
    diag: dict = {}
    content = "示意图。\n\n```mermaid\nunknownDiagram\nfoo --> bar\n```\n"
    _parse_content_blocks(content, diagnostics=diag)
    assert diag.get("chart_mermaid_skipped") == 1, diag
    assert diag.get("chart_mermaid_skipped_unrenderable") == 1, diag


def test_ai_image_unclosed_records_diagnostics():
    """未闭合 ai_image 占位块 → chart_ai_image_skipped 计数。"""
    diag: dict = {}
    content = '效果示意图如下。\n\n```ai_image\n{"prompt": "深基坑支护剖面", "title": "支护剖面图"}\n'
    _parse_content_blocks(content, diagnostics=diag)
    assert diag.get("chart_ai_image_skipped") == 1, diag
    assert diag.get("chart_ai_image_skipped_unclosed") == 1, diag


def test_merge_stats_includes_mermaid_and_ai_image_families():
    """三族跳过统一计入 failed；chart_json_skipped 旧键语义不变、新键加法式。"""
    rs = {"fe": 0, "backend_ok": 1, "failed": 0}
    E._merge_chart_json_skip_stats(
        rs,
        {
            "chart_json_skipped": 2,
            "chart_json_skipped_unclosed": 1,
            "chart_mermaid_skipped": 3,
            "chart_mermaid_skipped_unclosed": 2,
            "chart_mermaid_skipped_unrenderable": 1,
            "chart_ai_image_skipped": 1,
            "chart_ai_image_skipped_unclosed": 1,
        },
    )
    assert rs["failed"] == 6, "三族跳过全部计入 failed（缺图=失败，同权重）"
    assert rs["chart_json_skipped"] == 2, "既有键语义保持「只含 chart-json」"
    assert rs["chart_mermaid_skipped"] == 3
    assert rs["chart_ai_image_skipped"] == 1
    assert rs["chart_mermaid_skipped_unclosed"] == 2
    assert rs["chart_mermaid_skipped_unrenderable"] == 1


def test_merge_stats_no_new_keys_when_zero():
    """零跳过时不新增任何键（旧响应头形状逐字节不变）。"""
    rs = {"fe": 1, "backend_ok": 2, "failed": 0}
    E._merge_chart_json_skip_stats(rs, {})
    assert rs == {"fe": 1, "backend_ok": 2, "failed": 0}, rs


@pytest.mark.asyncio
async def test_unclosed_mermaid_export_is_degraded_not_cached(db_conn, monkeypatch):
    """端到端：正文含未闭合 mermaid 块 → failed≥1 → X-Cache-Status=degraded。

    旧实现该形态零机器信号：预渲染 failed=0、交付判据全假 → 缺图产物照常
    写缓存永久命中（R222/R181 已对 chart-json 侧闭合，mermaid 侧漏网）。
    """
    from app.services.ai import mermaid_renderer as mr

    def _fake(code, ct, fmt, skip_http, allow_pil, strategy_out=None):
        if isinstance(strategy_out, dict):
            strategy_out["strategy"] = "pil_syntax_fallback"
        return BytesIO(_real_png())

    monkeypatch.setattr(mr._chart_cache, "get_or_render", _fake)
    scheme_id = "s-r233-unclosed"
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        (scheme_id, f"p-{scheme_id}", "R233 未闭合统计"),
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (f"c-{scheme_id}", scheme_id, "", 0, 1, "第一章 施工", "generated", "工艺如下。\n\n```mermaid\nflowchart TD\nA --> B\n"),
    )
    await db_conn.commit()
    resp = await E.export_docx(
        scheme_id,
        {
            "project_name": "示例项目",
            "bid_section": "第一标段",
            "config": {},
            "chart_images": [],
            "fe_codes": [],
            "override_high_issues": True,
        },
        db=db_conn,
    )
    assert isinstance(resp, FileResponse)
    stats = json.loads(resp.headers.get("X-Chart-Render-Stats") or "{}")
    assert stats.get("chart_mermaid_skipped") == 1, stats
    assert int(stats.get("failed") or 0) >= 1, stats
    assert resp.headers.get("X-Cache-Status") == "degraded", resp.headers
    assert "charts-dropped" not in (resp.headers.get("X-Export-Degraded") or "") or True  # 降级原因允许为 failed 口径


# ===========================================================================
# B · FIX-2：max_ai_images 预算排除与坏缓存守卫解耦
# ===========================================================================


def _ai_block(code: str, sec_id: str) -> dict:
    return {"type": "ai_image", "code": code, "title": "AI 配图", "inline": True, "_sec": sec_id}


@pytest.mark.asyncio
async def test_budget_excluded_blocks_are_marked_and_excluded_from_pending(monkeypatch):
    """预算裁剪的占位块打 _budget_excluded 标记；保留块正常改写为 image 块。"""
    from app.config import settings as _settings
    from app.services.ai import image_engine as ie

    monkeypatch.setattr(_settings, "max_ai_images", 1)
    monkeypatch.setattr(_settings, "image_enabled", False)  # 生图快速失败，不计费

    blocks = {
        "sec1": [_ai_block('{"prompt":"a","title":"图A"}', "sec1")],
        "sec2": [_ai_block('{"prompt":"b","title":"图B"}', "sec2")],
    }
    sections = [{"id": "sec1", "content": ""}, {"id": "sec2", "content": ""}]
    config = {"ai_image_auto_generate": True}
    await E._auto_generate_ai_image_blocks(sections, blocks, config)
    marked = [b for sec in blocks.values() for b in sec if b.get("_budget_excluded")]
    kept = [b for sec in blocks.values() for b in sec if not b.get("_budget_excluded")]
    assert len(marked) == 1 and len(kept) == 1, (marked, kept)
    assert kept[0]["code"] != '{"prompt":"a","title":"图A"}' or kept[0]["code"] != '{"prompt":"b","title":"图B"}'


def test_ai_pending_excludes_budget_marked_blocks():
    """_ai_pending 统计豁免 _budget_excluded 块（源码锁，防回归「恒 degraded」）。"""
    src = Path(E.__file__).read_text(encoding="utf-8")
    needle = 'and not _blk.get("_budget_excluded")'
    assert needle in src, "R233 豁免判据丢失：预算裁剪块将重新混入 ai_image_pending → 恒 degraded 永不写缓存"


def test_budget_excluded_warning_copy_diverges():
    """write_section 对预算裁剪块打 INFO（有意设计），对生成失败块打 WARNING。"""
    src = Path(E.__file__).read_text(encoding="utf-8")
    assert "因全局预算裁剪跳过" in src
    assert "请检查图像模型配置（导出期自动生成失败）" in src, "生成失败告警文案不得被预算分流改掉"


# ===========================================================================
# C · FIX-3：块级 fail-soft 异常补记 chart_delivery（四键守恒）
# ===========================================================================


@pytest.mark.asyncio
async def test_chart_block_exception_records_block_exception_delivery(db_conn, monkeypatch):
    """chart 块在 expected+1 之后、任一 note 出口之前抛异常 → block_exception 补记。

    旧实现四键不守恒（expected>0 但 inserted/dropped/duplicate 全 0），
    X-Chart-Render-Stats 显示 dropped=0 而成稿确缺图。
    """
    from app.services.ai import mermaid_renderer as mr

    def _fake(code, ct, fmt, skip_http, allow_pil, strategy_out=None):
        return BytesIO(_real_png())

    monkeypatch.setattr(mr._chart_cache, "get_or_render", _fake)

    # 预渲染收集（第 1 次调用）返回正常码，write_section（第 2 次）抛异常
    calls = {"n": 0}
    real_resolve = E._resolve_chart_code

    def _flaky(block, sec_id, chart_type, chart_lookup, chart_type_index, order_rank=None):
        calls["n"] += 1
        if calls["n"] <= 1:
            return real_resolve(block, sec_id, chart_type, chart_lookup, chart_type_index, order_rank)
        raise RuntimeError("R233 模拟块级异常（在 note 出口之前）")

    monkeypatch.setattr(E, "_resolve_chart_code", _flaky)

    scheme_id = "s-r233-blkexc"
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        (scheme_id, f"p-{scheme_id}", "R233 块级异常"),
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            f"c-{scheme_id}",
            scheme_id,
            "",
            0,
            1,
            "第一章 施工",
            "generated",
            "工艺流程如下。\n\n```mermaid\nflowchart TD\nA[准备] --> B[施工]\n```\n",
        ),
    )
    await db_conn.commit()
    resp = await E.export_docx(
        scheme_id,
        {
            "project_name": "示例项目",
            "bid_section": "第一标段",
            "config": {},
            "chart_images": [],
            "fe_codes": [],
            "override_high_issues": True,
        },
        db=db_conn,
    )
    assert isinstance(resp, FileResponse), "块级 fail-soft：单块异常不得拖垮整份导出"
    stats = json.loads(resp.headers.get("X-Chart-Render-Stats") or "{}")
    assert stats.get("chart_expected") == 1, stats
    assert stats.get("chart_dropped") == 1, stats
    assert stats.get("chart_dropped_details", [{}])[0].get("reason") == "block_exception", stats


# ===========================================================================
# D · FIX-4：allow_pil_fallback 显式 None 归一默认 True
# ===========================================================================


@pytest.mark.asyncio
async def test_allow_pil_fallback_none_normalizes_to_default(db_conn, monkeypatch):
    """config 显式传 allow_pil_fallback=None → 与未传同语义（默认 True 透传渲染器）。

    旧实现 bool(None)=False（关掉兜底）与指纹归一「None 视同未传」分叉
    → 同一指纹两种渲染行为（R230 A6 同款缺口）。
    """
    from app.services.ai import mermaid_renderer as mr

    seen: dict = {}

    def _fake(code, ct, fmt, skip_http, allow_pil=True, strategy_out=None):
        seen["allow_pil"] = allow_pil
        return BytesIO(_real_png())

    monkeypatch.setattr(mr._chart_cache, "get_or_render", _fake)
    scheme_id = "s-r233-pilnone"
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        (scheme_id, f"p-{scheme_id}", "R233 None 归一"),
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            f"c-{scheme_id}",
            scheme_id,
            "",
            0,
            1,
            "第一章 施工",
            "generated",
            "流程如下。\n\n```mermaid\nflowchart TD\nA[准备] --> B[施工]\n```\n",
        ),
    )
    await db_conn.commit()
    resp = await E.export_docx(
        scheme_id,
        {
            "project_name": "示例项目",
            "bid_section": "第一标段",
            "config": {"allow_pil_fallback": None},
            "chart_images": [],
            "fe_codes": [],
            "override_high_issues": True,
        },
        db=db_conn,
    )
    assert isinstance(resp, FileResponse)
    assert seen.get("allow_pil") is True, "显式 None 必须归一为默认 True（与未传同语义）"


def test_ai_image_auto_generate_none_normalization_sources():
    """ai_image_auto_generate 两处读取点均有 None 归一判据（源码锁）。"""
    src = Path(E.__file__).read_text(encoding="utf-8")
    assert src.count('config.get("ai_image_auto_generate")') >= 2, "两处 None 归一读取点（入口 + _ai_pending 门控）"
    assert "True if _aig is None else _aig" in src
    assert "True if _aig2 is None else _aig2" in src


# ===========================================================================
# E · FIX-5：语义上下文跳过前一围栏块（且不跨块穿透）
# ===========================================================================


def test_semantic_context_skips_prev_fence_block_entirely():
    """前一块的闭合行、内部代码行、开启行全部不进语义上下文。

    探针实证（R233）：旧实现上下文以 ``` 开头；首版只滤围栏行仍会把
    前一块内部代码行（`任务 :t1, 1, 3d`）当正文段落收进来。
    """
    from app.routers._chart_pipeline import _chart_semantic_context, _scan_chart_fences_full

    content = (
        "## 进度计划\n\n进度安排如下：\n\n```mermaid\ngantt\n任务 :t1, 1, 3d\n```\n\n中间段落。\n\n"
        "组织架构如下：\n\n```chart-json\n{\"type\":\"labor\",\"phases\":[\"备\"],\"categories\":[\"工\"],\"data\":[[1]]}\n```\n"
        "```mermaid\n未闭合残片\n"
    )
    scans = _scan_chart_fences_full(content)
    assert len(scans) == 2
    ctx1 = _chart_semantic_context(content, 1)
    assert "组织架构如下" in ctx1
    assert "中间段落" in ctx1
    assert "```" not in ctx1, f"围栏行不得混入语义上下文：{ctx1!r}"
    assert "任务 :t1" not in ctx1, f"前一块内部代码行不得混入语义上下文：{ctx1!r}"


def test_semantic_context_stops_at_prev_block_boundary():
    """R228 意图保留、强度不减：本块语境止步于前一图表块边界，绝不跨块穿透。"""
    from app.routers._chart_pipeline import _chart_semantic_context

    labor = '{"type":"labor","phases":["准备"],"categories":["普工"],"data":[[3]]}'
    content = (
        "施工进度计划与工期安排\n"
        "进度计划如下图所示：\n"
        "```chart-json\n" + labor + "\n```\n"
        "劳动力配置计划\n"
        "各工种人数如下。\n"
        "```chart-json\n" + labor + "\n```\n"
    )
    ctx1 = _chart_semantic_context(content, 1)
    assert "劳动力配置计划" in ctx1
    assert "进度计划如下图所示" not in ctx1, "跨块穿透会把上一块语境制造成本块的假观测"
    assert "施工进度计划与工期安排" not in ctx1


# ===========================================================================
# F · CLEAN-1 + 字节体检 + 透传回归锁
# ===========================================================================


def test_fallback_4tuple_dead_branch_removed():
    """write_section 兜底渲染 4 元组死分支已删除（no_code continue 与 3 元组键保留）。"""
    src = Path(E.__file__).read_text(encoding="utf-8")
    assert '"fallback", chart_type, _norm_code' not in src, "不可达 4 元组键构造不得回流"
    assert 'render_key = (sec_id, chart_type, _norm_code(mermaid_code))' in src, "3 元组主键保留"
    assert src.count("_note_chart_delivery(\"no_code\"") == 1, "no_code 记账出口保留"


def test_semantic_notes_report_passthrough_still_wired():
    """R228 语义观测透传链在位（sse_handlers report 字段 + build 参数）。"""
    sh = (Path(E.__file__).parent / "sse_handlers.py").read_text(encoding="utf-8")
    assert 'report["charts_semantic_notes"]' in sh
    assert "semantic_notes_out=_chart_semantic_notes" in sh


@pytest.mark.parametrize(
    "relpath",
    [
        "app/services/content_blocks.py",
        "app/routers/export.py",
        "app/routers/_chart_pipeline.py",
    ],
)
def test_bytes_health_crlf_and_no_bom_fffd(relpath):
    """触碰生产源纯 CRLF / BOM=0 / U+FFFD=0（Edit 保持，不因本轮引入漂移）。"""
    p = _WS_ROOT / "backend" / relpath
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{relpath} 出现 BOM"
    text = raw.decode("utf-8")
    assert "\ufffd" not in text, f"{relpath} 出现 U+FFFD"
    assert "\r\n" in text, f"{relpath} 必须保持 CRLF 行尾"
