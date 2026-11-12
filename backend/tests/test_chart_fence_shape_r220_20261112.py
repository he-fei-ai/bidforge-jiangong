# -*- coding: utf-8 -*-
"""R220（2026-11-12）图表生成与导出渲染 · 畸形围栏形态修复护栏。

动因（生产库 ``scheme_assistant.db`` 实测 + 导出 DOCX 取证）：

① 形态 A（sections 3c150ba0「井道尺寸垂直度验收」）：AI 把流程图包进单列
   「图题假表格」—— ``| 图 9-1 … |`` / ``| :---: |`` / ``| ```mermaid `` …
   `` ``` |``。开启行以管道开头 → ``parse_fence_line`` 判非围栏 → **图表从不
   渲染**，Mermaid 源码以表格单元格/裸段落形态泄漏进成稿；闭合行 `` ``` |``
   信息串非空 → 被当成 ``lang="|"`` 的代码围栏，图注段被吞进代码块。

② 形态 B（sections a0b4093e「专职安全员现场巡查职责」）：AI 把**整段正文**
   包进 `` ```markdown `` 文档围栏，段中内嵌 `` ```chart-json `` 图表 ——
   正文整体退化为一个代码块（成稿 run-on 纯文本、``###`` 标记原样印刷、
   标题/表格/列表结构全丢），内嵌图表被外层围栏遮蔽 → 解析与登记两侧全部丢图。

手法：行为实证（真实解析 / 真实登记扫描 / ordinal 改写锚点）+ **反证**
（锁住「修前确实丢图」前提，防止护栏变恒真）+ 行数不变棘轮（numbering
``src_line`` 契约，R192 FIX-R1 家族）+ 幂等 + 既有行为保护（无图表的
markdown 围栏 / 真实表格 / ```python 围栏零触碰）。
"""
from __future__ import annotations

import logging

import pytest
from app.routers._chart_pipeline import (
    _apply_chart_fence_edits,
    _scan_chart_fences_full,
    iter_inline_chart_fences,
)
from app.services.content_blocks import _parse_content_blocks, normalize_chart_fence_shapes

# ---------------------------------------------------------------------------
# 生产取证形态（逐字取自 scheme_assistant.db，只保留判据所需骨架）
# ---------------------------------------------------------------------------

#: 形态 A：图题假表格包裹 mermaid 流程图（3c150ba0 骨架）
FIG_TABLE_SHAPE = (
    "壁面平整度验收要点如上文所述。\n"
    "\n"
    "| 图 9-1 井道垂直度检测示意图 |\n"
    "| :---: |\n"
    "| ```mermaid\n"
    "flowchart TD\n"
    " A[设定底部基准点] --> B[顶部架设激光垂准仪]\n"
    " B --> C[投射垂直线至各层角点]\n"
    " C --> D{偏差是否超标?}\n"
    " D -->|否| E[记录数据]\n"
    "``` |\n"
    "\n"
    "注：图中流程展示了垂直度检测的逻辑闭环。\n"
)

#: 形态 B：```markdown 文档围栏包裹正文 + 内嵌 chart-json（a0b4093e 骨架）
DOC_FENCE_SHAPE = (
    "```markdown\n"
    "### 8.1.2 专职安全员现场巡查职责\n"
    "\n"
    "专职安全员是本项目现场安全管控的第一责任人，需持有安全管理人员C证。\n"
    "\n"
    "1. 每日定点巡查与动态监测\n"
    "2. 特种作业人员资质核验\n"
    "\n"
    "```chart-json\n"
    '{"type":"flowchart","title":"专职安全员日常巡查流程","steps":[{"id":"s1","label":"班前交底"}],'
    '"edges":[]}\n'
    "```\n"
    "\n"
    "巡查频次执行两巡三查制度。\n"
)

#: 形态 B 的未闭合外层变体（AI 忘写外层闭合围栏；内层图表闭合行即最后一个 ```）
DOC_FENCE_SHAPE_UNCLOSED = (
    "```markdown\n"
    "### 8.1.2 专职安全员现场巡查职责\n"
    "\n"
    "专职安全员是本项目现场安全管控的第一责任人。\n"
    "\n"
    "```chart-json\n"
    '{"type":"flowchart","title":"巡查流程","steps":[{"id":"s1","label":"交底"}],"edges":[]}\n'
    "```\n"
    "\n"
    "巡查频次执行两巡三查制度。\n"
)


def _charts(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if b.get("type") == "chart"]


def _chart_codes(blocks: list[dict]) -> list[str]:
    return [b.get("code", "") for b in _charts(blocks)]


# =====================================================================
# A 组 · 形态 A（图题假表格）修复
# =====================================================================
class TestFigureTableShape:
    def test_chart_recovered_and_source_not_leaked(self):
        """图表围栏必须恢复为 chart 块；源码不得以表格行/段落形态泄漏。"""
        blocks = _parse_content_blocks(FIG_TABLE_SHAPE)
        charts = _charts(blocks)
        assert len(charts) == 1, f"图表必须恢复，实得块类型 {[b['type'] for b in blocks]}"
        assert charts[0]["chart_type"] == "flowchart"
        assert "flowchart TD" in charts[0]["code"]
        # 假表格必须消失（真实表格不在此形态里）
        assert all(b["type"] != "table" for b in blocks), "伪图题表格必须被解包"
        # 围栏标记不得残留在任何块文本里
        for b in blocks:
            text = str(b.get("text") or "") + "".join(b.get("lines") or [])
            assert "```" not in text, f"围栏标记泄漏：{text[:60]}"

    def test_caption_absorbed_as_chart_title(self):
        """图题行必须升格为图表 title（防源文自写图号与导出图号重号）。

        生产形态里图题在假表格单元格内（不是段落）→ 解析侧图题吸收够不着；
        解包后它还原为紧邻图表上方的段落 → 既有 ``_extract_figure_caption``
        立即把它吸收为 ``title``，源文自写「图 9-1」编号前缀同时被剥。
        """
        blocks = _parse_content_blocks(FIG_TABLE_SHAPE)
        charts = _charts(blocks)
        assert len(charts) == 1
        assert charts[0].get("title") == "井道垂直度检测示意图", (
            f"图题必须被吸收为图表 title，实得 {charts[0].get('title')!r}"
        )
        assert not any(
            b.get("type") == "paragraph" and "井道垂直度检测示意图" in str(b.get("text"))
            for b in blocks
        ), "图题已升格，正文中不得再残留重复段落"

    def test_registration_side_sees_chart(self):
        """登记侧扫描必须与导出解析同口径看见图表（幽灵登记反向防线）。"""
        scanned = _scan_chart_fences_full(FIG_TABLE_SHAPE)
        assert [ct for ct, _code, _o in scanned] == ["flowchart"]

    def test_close_line_no_longer_opens_pipe_fence(self):
        """闭合行 `` ``` |`` 不得再产出 lang="|" 代码块（图注段不再被吞）。"""
        blocks = _parse_content_blocks(FIG_TABLE_SHAPE)
        codes = [b for b in blocks if b.get("type") == "code"]
        assert codes == [], f"不得残留代码块：{[b.get('lang') for b in codes]}"
        assert any(
            b.get("type") == "paragraph" and "逻辑闭环" in str(b.get("text"))
            for b in blocks
        ), "图注段必须还原为段落"

    def test_normalize_is_idempotent_and_keeps_line_count(self):
        """幂等 + 行数不变（numbering src_line 行号契约，R192 FIX-R1 家族）。"""
        once = normalize_chart_fence_shapes(FIG_TABLE_SHAPE)
        twice = normalize_chart_fence_shapes(once)
        assert twice == once, "形态修复不幂等"
        assert once.count("\n") == FIG_TABLE_SHAPE.count("\n"), "行数不得变化（src_line 契约）"

    def test_real_table_with_inline_mermaid_mention_untouched(self):
        """真实表格里**带其它文字**的 mermaid 字样不得触发解包（防误伤真实表格）。"""
        content = (
            "| 图表 | 说明 |\n"
            "| --- | --- |\n"
            '| 使用 ```mermaid 渲染 | 支持 |\n'
            "| 流程图 | 如下 |\n"
        )
        out = normalize_chart_fence_shapes(content)
        assert out == content, "真实表格不得被改写"

    def test_incomplete_shape_untouched(self):
        """找不到闭合行（形态不完整）→ 保持原样，不冒险重写半个形态。"""
        content = (
            "| 图 1 流程 |\n"
            "| :---: |\n"
            "| ```mermaid\n"
            "flowchart TD\n"
            " A --> B\n"
        )
        assert normalize_chart_fence_shapes(content) == content


# =====================================================================
# B 组 · 形态 B（文档围栏包裹正文 + 内嵌图表）修复
# =====================================================================
class TestDocFenceShape:
    def test_chart_recovered_and_body_restored(self):
        """内嵌图表必须恢复为 chart 块；正文必须还原为结构化块（标题/列表/段落）。"""
        blocks = _parse_content_blocks(DOC_FENCE_SHAPE)
        charts = _charts(blocks)
        assert len(charts) == 1, f"内嵌 chart-json 必须恢复，实得 {[b['type'] for b in blocks]}"
        assert charts[0]["chart_type"] == "flowchart"
        assert "专职安全员日常巡查流程" in charts[0]["code"]
        types = {b["type"] for b in blocks}
        assert "heading" in types, "被吞的正文标题必须还原为 heading 块"
        assert "list_item" in types, "被吞的列表必须还原"
        # ### 标记不得原样印刷（run-on 根因）
        for b in blocks:
            if b.get("type") == "paragraph":
                assert not str(b.get("text", "")).startswith("###"), "### 标记不得残留在正文段落"

    def test_registration_side_sees_nested_chart(self):
        """登记侧必须穿透文档围栏看见内嵌图表（否则幽灵登记反向分叉）。"""
        scanned = _scan_chart_fences_full(DOC_FENCE_SHAPE)
        assert [ct for ct, _code, _o in scanned] == ["flowchart"]

    def test_unclosed_outer_fence_also_recovered(self):
        """外层围栏未闭合（AI 忘写）→ 只抹开启行，内嵌图表照常恢复。"""
        blocks = _parse_content_blocks(DOC_FENCE_SHAPE_UNCLOSED)
        charts = _charts(blocks)
        assert len(charts) == 1 and charts[0]["chart_type"] == "flowchart"

    def test_normalize_is_idempotent_and_keeps_line_count(self):
        once = normalize_chart_fence_shapes(DOC_FENCE_SHAPE)
        twice = normalize_chart_fence_shapes(once)
        assert twice == once
        assert once.count("\n") == DOC_FENCE_SHAPE.count("\n"), "行数不得变化（src_line 契约）"

    def test_plain_markdown_fence_without_chart_untouched(self):
        """**无图表**的 markdown 围栏必须保持既有行为（按代码块渲染，零触碰）。

        这同时是 numbering 侧「围栏内标题不参与重编号」契约的前提
        （test_outline_generation_gaps_20261008::D-8g 场景）。
        """
        content = "```markdown\n＃＃ 围栏内全角井号\n## 围栏内标题\n```\n\n## 真标题\n"
        assert normalize_chart_fence_shapes(content) == content

    def test_python_fence_untouched(self):
        """普通 ```python 围栏零触碰。"""
        content = "```python\nprint('x')\n```\n\n正文。\n"
        assert normalize_chart_fence_shapes(content) == content

    def test_src_line_alignment_after_unwrap(self):
        """解包后行号必须仍与源文对齐（重写不会落到错误行）。

        解包只置空标记行（行数不变），其后标题的 src_line 与原文行表一一对应。
        """
        content = DOC_FENCE_SHAPE + "\n### 子标题A\n\n### 子标题B\n"
        blocks = _parse_content_blocks(content)
        heads = [b for b in blocks if b.get("type") == "heading"]
        tail = [b for b in heads if b.get("text") in ("子标题A", "子标题B")]
        assert len(tail) == 2, "尾随标题必须被解析为 heading"
        srcs = [b["src_line"] for b in tail]
        assert srcs[0] < srcs[1]
        for b in tail:
            assert content.split("\n")[b["src_line"]].strip().startswith("###"), (
                f"src_line 错位：第 {b['src_line']} 行不是该标题原文"
            )


# =====================================================================
# C 组 · 消费方贯通（登记 / ordinal 改写锚点 / 反证）
# =====================================================================
class TestWiringAndCounterEvidence:
    def test_counter_evidence_old_parse_loses_chart(self):
        """反证：把形态修复短路成恒等后，旧解析确实丢图（前提锁定，防恒真）。"""
        import app.services.content_blocks as cb

        original = cb.normalize_chart_fence_shapes
        try:
            cb.normalize_chart_fence_shapes = lambda c: c
            blocks = cb._parse_content_blocks(DOC_FENCE_SHAPE)
            assert _charts(blocks) == [], "反证前提：旧解析确实丢失内嵌图表"
            blocks_a = cb._parse_content_blocks(FIG_TABLE_SHAPE)
            assert _charts(blocks_a) == [], "反证前提：旧解析确实丢失假表格中的图表"
        finally:
            cb.normalize_chart_fence_shapes = original

    def test_iter_and_apply_ordinals_stay_consistent(self):
        """ordinal 改写锚点：扫描与改写必须在同一份修复后文本上计数。"""
        scanned = _scan_chart_fences_full(DOC_FENCE_SHAPE)
        assert len(scanned) == 1
        ordinal = scanned[0][2]
        edited = _apply_chart_fence_edits(
            DOC_FENCE_SHAPE, {ordinal: '{"type":"flowchart","title":"修复后","steps":[],"edges":[]}'}
        )
        assert "修复后" in edited, "ordinal 改写必须命中修复后文本里的目标块"
        assert "班前交底" not in edited, "旧代码必须被替换"

    def test_iter_inline_chart_fences_sees_recovered_chart(self):
        items = iter_inline_chart_fences(FIG_TABLE_SHAPE)
        assert [(lang, state) for lang, _code, state, _o in items] == [("mermaid", "closed")]


# =====================================================================
# D 组 · 导出器版本棘轮 + 字节体检
# =====================================================================
class TestR220Hygiene:
    def test_exporter_version_bumped_for_render_change(self):
        """渲染输出变化必须整体失效导出缓存（_EXPORTER_VERSION ≥ 25）。"""
        from app.routers import export as ex

        assert int(ex._EXPORTER_VERSION) >= 25, (
            "解析层图表恢复改变了渲染产物，_EXPORTER_VERSION 未 bump → 旧缓存命中旧缺图版式"
        )

    def test_new_sources_crlf_and_no_replacement_chars(self):
        """本轮触碰的生产源文件字节体检（CRLF 保持 / 无 BOM / 无 U+FFFD）。"""
        from pathlib import Path

        for rel in (
            "app/services/content_blocks.py",
            "app/routers/_chart_pipeline.py",
        ):
            raw = (Path(__file__).resolve().parents[1] / rel).read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 带 BOM"
            assert raw.count(b"\xef\xbf\xbd") == 0, f"{rel} 含 U+FFFD"
            crlf = raw.count(b"\r\n")
            lf = raw.count(b"\n")
            assert crlf == lf, f"{rel} 行尾混用（CRLF={crlf} LF={lf}）"

    def test_r220_marker_present_in_sources(self):
        """源码棘轮：两处消费方接线不得被摘除。"""
        from pathlib import Path

        cb_src = (Path(__file__).resolve().parents[1] / "app/services/content_blocks.py").read_text(encoding="utf-8")
        cp_src = (Path(__file__).resolve().parents[1] / "app/routers/_chart_pipeline.py").read_text(encoding="utf-8")
        assert "content = normalize_chart_fence_shapes(content)" in cb_src
        assert "content = normalize_chart_fence_shapes(content)" in cp_src
        assert cp_src.count("normalize_chart_fence_shapes") >= 3, "登记/改写两处接线不得缺失"

    def test_no_log_noise_on_untouched_content(self, caplog):
        """正常正文（无畸形形态）不得产生 R220 告警（防刷屏）。"""
        content = (
            "# 标题\n\n正文段落。\n\n```mermaid\nflowchart TD\n A --> B\n```\n\n尾段。\n"
        )
        with caplog.at_level(logging.WARNING, logger="content_blocks"):
            blocks = _parse_content_blocks(content)
        r220_logs = [r for r in caplog.records if "R220" in r.getMessage()]
        assert len(_charts(blocks)) == 1
        assert r220_logs == [], f"正常正文不得触发 R220 告警：{[r.getMessage() for r in r220_logs]}"
