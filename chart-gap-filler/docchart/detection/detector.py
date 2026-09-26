# -*- coding: utf-8 -*-
"""缺失图表检测器：调度各规则、计算置信度、去重并输出 ChartGap 清单。"""

from __future__ import annotations

import logging
import re
from bisect import bisect_left
from typing import Optional

from ..config import Config
from ..models import (Block, BlockKind, ChartGap, Document, GapType,
                      PLACEHOLDER_RE, figure_caption_of)
from . import rules as R

logger = logging.getLogger("docchart.detect")

# 建议展示的最小步骤数（流程图）
_MIN_FLOW_STEPS = 3


class Detector:
    def __init__(self, doc: Document, cfg: Optional[Config] = None):
        self.doc = doc
        self.cfg = cfg or Config()
        self.blocks = doc.blocks
        # 预计算：图注编号集合、图片块列表
        self.caption_nums: dict[str, Block] = {}
        self.image_ids: set[int] = set()
        for b in self.blocks:
            if b.kind == BlockKind.CAPTION:
                cap = figure_caption_of(b.text)
                if cap:
                    self.caption_nums[cap[0]] = b
            elif b.kind == BlockKind.IMAGE:
                self.image_ids.add(b.id)
        # 预排序图片 id，供 _chart_near 二分查找（长文档避免 O(n²) 扫描）
        self._image_ids_sorted = sorted(self.image_ids)
        self.window = int(self.cfg.get("detection", "ref_window_blocks", 12))

    # ------------------------------------------------------------------
    def detect(self) -> list[ChartGap]:
        gaps: list[ChartGap] = []
        gaps += self._rule_dangling_refs()
        gaps += self._rule_caption_no_image()
        gaps += self._rule_placeholders()
        gaps += self._rule_tables_no_chart()
        gaps += self._rule_broken_numbering()
        if self.cfg.get("detection", "semantic_hint", True):
            gaps += self._rule_semantic()
        gaps = self._dedup(gaps)
        min_c = float(self.cfg.get("detection", "min_confidence", 0.35))
        gaps = [g for g in gaps if g.confidence >= min_c]
        # 补充章节归属，便于报告阅读
        for g in gaps:
            b = self.blocks[g.anchor_block_id]
            g.section = b.section or self.doc.nearest_heading(g.anchor_block_id)
        gaps.sort(key=lambda g: g.anchor_block_id)
        logger.info("检测完成：%d 处缺失", len(gaps))
        return gaps

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _chart_near(self, block_id: int, forward_only: bool = False) -> bool:
        """窗口内是否存在图片块（图已存在的统一判定）。"""
        lo = block_id if forward_only else block_id - self.window
        hi = block_id + self.window
        ids = self._image_ids_sorted
        i = bisect_left(ids, lo)
        # 图注存在且邻近位置有图即视为已配图；仅有图注交给 R2 精确处理
        return i < len(ids) and ids[i] <= hi

    def _dedup(self, gaps: list[ChartGap]) -> list[ChartGap]:
        """同一锚点+类型只保留置信度最高的一条。"""
        best: dict[tuple[int, str], ChartGap] = {}
        for g in gaps:
            key = (g.anchor_block_id, g.gap_type.value)
            if key not in best or g.confidence > best[key].confidence:
                best[key] = g
        # 锚点完全相同的不同规则，也只留最高置信一条，避免重复插图
        by_anchor: dict[int, ChartGap] = {}
        for g in sorted(best.values(), key=lambda x: -x.confidence):
            if g.anchor_block_id not in by_anchor:
                by_anchor[g.anchor_block_id] = g
        return list(by_anchor.values())

    # ------------------------------------------------------------------
    # R1: 引用悬空——"如图 4-2 所示/如下图所示"但找不到图
    # ------------------------------------------------------------------

    def _rule_dangling_refs(self) -> list[ChartGap]:
        gaps: list[ChartGap] = []
        for b in self.blocks:
            if b.kind not in (BlockKind.PARAGRAPH, BlockKind.LIST) or not b.text:
                continue
            # 带编号引用
            for m in R.REF_NUMBERED_RE.finditer(b.text):
                num = m.group(1).rstrip(".。")
                # 排除"图表/图形"等误伤已由正则限形；图号存在（有图注且附近有图）则跳过
                cap = self.caption_nums.get(num)
                if cap is not None and self._chart_near(cap.id):
                    continue
                if cap is not None:
                    continue  # 图注存在但无图，交给 R2 精确处理，避免重复
                hint = R.suggest_type_from_text(b.text)
                ctype = hint[0] if hint else "flow"
                prefix = str(self.cfg.get("output", "caption_prefix", "图"))
                gaps.append(ChartGap(
                    anchor_block_id=b.id, gap_type=GapType.DANGLING_REF,
                    reason=f"正文引用「图{num}」，但文档中不存在该编号的图注/图片",
                    confidence=0.92, suggested_type=ctype,
                    caption=f"{prefix}{num} {self.cfg.get('output', 'auto_caption_suffix', '（AI 补全）')}",
                    needed_data=hint[1] if hint else "引用处上下文对应的可视化数据",
                ))
                break  # 每段只报一次
            # 无编号引用：如下图所示
            if R.REF_UNNUMBERED_RE.search(b.text) and not self._chart_near(b.id, forward_only=True):
                hint = R.suggest_type_from_text(b.text)
                ctype, need = hint if hint else ("flow", "从上下文抽取的步骤或数值")
                # 流程链常在引用句的后续段落，扩大抽取窗口至后 3 块
                ctx_after = " ".join(
                    x.text for x in self.blocks[b.id + 1: b.id + 4]
                    if x.kind in (BlockKind.PARAGRAPH, BlockKind.LIST))
                steps = R.extract_flow_steps(b.text) or R.extract_flow_steps(ctx_after)
                gaps.append(ChartGap(
                    anchor_block_id=b.id, gap_type=GapType.DANGLING_REF,
                    reason="正文出现「如下图所示/见下图」等引用，但后文窗口内无任何图片",
                    confidence=0.85, suggested_type=ctype,
                    caption=b.section.split(" > ")[-1][:18] or "上下文示意图",
                    needed_data=need,
                    steps=steps,
                ))
        return gaps

    # ------------------------------------------------------------------
    # R2: 图注无图——存在"图 x-x 标题"但附近没有图片块
    # ------------------------------------------------------------------

    def _rule_caption_no_image(self) -> list[ChartGap]:
        gaps: list[ChartGap] = []
        for b in self.blocks:
            if b.kind != BlockKind.CAPTION:
                continue
            if self._chart_near(b.id):
                continue
            # 附近文本给出提示类型
            ctx = " ".join(x.text for x in self.blocks
                           if abs(x.id - b.id) <= 3 and x.kind == BlockKind.PARAGRAPH)
            hint = R.suggest_type_from_text(ctx)
            ctype = hint[0] if hint else "bar"
            steps = R.extract_flow_steps(ctx) if ctype == "flow" else []
            gaps.append(ChartGap(
                anchor_block_id=b.id, gap_type=GapType.CAPTION_NO_IMAGE,
                reason=f"图注「{b.text[:30]}」存在，但其前后均无实际图片",
                confidence=0.9, suggested_type=ctype,
                caption=b.text[:40], needed_data="按图注与所在章节语义补充对应图表",
                steps=steps, insert_before=True,
            ))
        return gaps

    # ------------------------------------------------------------------
    # R3: 空图表占位符
    # ------------------------------------------------------------------

    def _rule_placeholders(self) -> list[ChartGap]:
        gaps: list[ChartGap] = []
        for b in self.blocks:
            if b.kind not in (BlockKind.PARAGRAPH, BlockKind.LIST):
                continue
            m = PLACEHOLDER_RE.search(b.text or "")
            if m and not self._chart_near(b.id):
                hint = R.suggest_type_from_text(b.text)
                ctype, need = hint if hint else ("bar", "占位符所在小节对应的量化数据")
                gaps.append(ChartGap(
                    anchor_block_id=b.id, gap_type=GapType.EMPTY_PLACEHOLDER,
                    reason=f"存在空图表占位符「{m.group(0)}」，未落实为实际图表",
                    confidence=0.95, suggested_type=ctype,
                    caption=b.section.split(" > ")[-1][:18] or "占位图表",
                    needed_data=need,
                ))
        return gaps

    # ------------------------------------------------------------------
    # R4: 有表无图——表格含足量数值但无可视化配套
    # ------------------------------------------------------------------

    def _rule_tables_no_chart(self) -> list[ChartGap]:
        gaps: list[ChartGap] = []
        min_rows = int(self.cfg.get("detection", "table_min_rows", 3))
        min_cols = int(self.cfg.get("detection", "table_min_numeric_cols", 1))
        for b in self.blocks:
            if b.kind != BlockKind.TABLE or len(b.rows) < min_rows:
                continue
            if self._chart_near(b.id):
                continue
            density, numeric_cols = R.table_numeric_density(b.rows)
            # 数据缺失判定：数值列之外的数据单元格大量【待补充】占位
            placeholder_heavy = R.table_todo_ratio(b.rows) >= 0.4
            if len(numeric_cols) < min_cols and not placeholder_heavy:
                continue
            conf = 0.45 + min(0.35, density)
            if placeholder_heavy:
                conf = 0.4  # 数据缺失仍提示，但降级并要求补数
            gaps.append(ChartGap(
                anchor_block_id=b.id, gap_type=GapType.TABLE_NO_CHART,
                reason=f"表格（{len(b.rows) - 1} 行 × {len(b.rows[0])} 列，数值密度 {density:.0%}）"
                       f"具备可视化价值，但附近无对应图表",
                confidence=conf, suggested_type="",  # 类型交由推荐器按数据形态定
                caption=(b.section.split(" > ")[-1][:18] or "数据") + "可视化",
                needed_data="将表格首列作为分类轴、数值列作为系列" if not placeholder_heavy
                            else "表格中大量【待补充】占位，需先补齐实测数据",
                needs_data=placeholder_heavy,
            ))
            # 锚点即表格块 id，生成阶段直接按锚点回查表格取数
        return gaps

    # ------------------------------------------------------------------
    # R6: 图号断裂——同一章节内图注编号序列有空缺（如 1-1、1-3，缺 1-2）
    # ------------------------------------------------------------------

    def _rule_broken_numbering(self) -> list[ChartGap]:
        gaps: list[ChartGap] = []
        # 按章前缀分组收集多级编号的图注（纯数字编号无法判断连续性，不参与）
        groups: dict[str, list[tuple[int, int]]] = {}
        for b in self.blocks:
            if b.kind != BlockKind.CAPTION:
                continue
            cap = figure_caption_of(b.text)
            if not cap:
                continue
            parts = re.split(r"[-—.．]", cap[0])
            if len(parts) < 2:
                continue
            try:
                seq = int(parts[-1])
            except ValueError:
                continue
            groups.setdefault("-".join(parts[:-1]), []).append((seq, b.id))
        for chapter, items in groups.items():
            items.sort(key=lambda x: x[0])
            present = {s for s, _ in items}
            for miss in range(min(present), max(present) + 1):
                if miss in present:
                    continue
                missing_no = f"{chapter}-{miss}"
                # 锚在空缺后的首个图注之前，便于人工就地核对
                anchor_id = next(bid for s, bid in items if s > miss)
                gaps.append(ChartGap(
                    anchor_block_id=anchor_id, gap_type=GapType.BROKEN_NUMBERING,
                    reason=f"图号不连续：章节「{chapter}」缺失「图{missing_no}」",
                    confidence=0.6, suggested_type="",
                    caption=f"图{missing_no}",
                    needed_data=(f"图号「图{missing_no}」在编号序列中缺失，"
                                 "可能漏排图或编号错位，请人工核对"),
                    needs_data=True, insert_before=True,
                ))
        return gaps

    # ------------------------------------------------------------------
    # R5: 语义建议——段落描述适合可视化且能就地取到数据/步骤
    # ------------------------------------------------------------------

    def _rule_semantic(self) -> list[ChartGap]:
        gaps: list[ChartGap] = []
        max_n = 8  # 防止长文档噪音爆炸
        for b in self.blocks:
            if len(gaps) >= max_n:
                break
            if b.kind not in (BlockKind.PARAGRAPH, BlockKind.LIST) or len(b.text) < 20:
                continue
            if R.REF_NUMBERED_RE.search(b.text) or R.REF_UNNUMBERED_RE.search(b.text):
                continue  # R1 已覆盖
            if self._chart_near(b.id):
                continue
            hint = R.suggest_type_from_text(b.text)
            if not hint:
                continue
            ctype, need = hint
            steps = R.extract_flow_steps(b.text)
            # 就地取数判定：段内至少出现两个可解析的数值（含单位）
            has_inline_numbers = len(re.findall(
                r"\d+(?:\.\d+)?\s*(?:%|吨|m²|㎡|m2|kN|MPa|N·m|mm|cm|元|人|台|个|天|层|根|跨|米)",
                b.text)) >= 2 or len(re.findall(r"\d+(?:\.\d+)?", b.text)) >= 4
            if ctype == "flow" and len(steps) < _MIN_FLOW_STEPS:
                continue
            if ctype != "flow" and not has_inline_numbers:
                continue  # 数值类建议必须能就地取数，否则噪音太大
            gaps.append(ChartGap(
                anchor_block_id=b.id, gap_type=GapType.SEMANTIC_HINT,
                reason=f"段落含「{ctype}」类可视化语义（趋势/占比/对比/流程…），"
                       f"且附近无图表，建议补图",
                confidence=0.5 if has_inline_numbers else 0.42,
                suggested_type=ctype,
                caption=b.section.split(" > ")[-1][:18] or "语义建议图",
                needed_data=need, steps=steps,
                needs_data=not has_inline_numbers,
            ))
        return gaps
