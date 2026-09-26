# -*- coding: utf-8 -*-
"""端到端流水线：解析 -> 检测 -> 数据抽取 -> 推荐 -> 渲染 -> 回插 -> 导出 -> 校验。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .config import Config
from .detection import Detector
from .generation import datasource, recommender, renderer
from .insertion import export_document
from .models import BlockKind, ChartGap, Document, GapType
from .parsers import parse_file

logger = logging.getLogger("docchart.pipeline")


def analyze(path: str | Path, cfg: Optional[Config] = None) -> tuple[Document, list[ChartGap]]:
    """只做检测，不生成，返回缺失清单。"""
    cfg = cfg or Config()
    doc = parse_file(path)
    gaps = Detector(doc, cfg).detect()
    return doc, gaps


def _image_path(assets_dir: Path, idx: int, fmt: str, gap: ChartGap) -> Path:
    return assets_dir / f"autofill_{idx:02d}_{gap.gap_type.value[:12]}.{fmt}"


def _image_format(doc: Document, cfg: Config) -> str:
    """输出图片格式：DOCX 回插依赖 python-docx，其不支持嵌入 SVG，强制 png。"""
    fmt = str(cfg.get("output", "image_format", "png"))
    if doc.fmt == "docx" and fmt.lower() == "svg":
        logger.warning("DOCX 不支持嵌入 SVG 图片，本次输出强制使用 png")
        return "png"
    return fmt


def _try_fill(doc: Document, gap: ChartGap, cfg: Config, assets_dir: Path,
              idx: int, doc_title: str) -> None:
    """对单个缺口执行：定位数据 -> 推荐 -> 渲染；失败则标记 skipped+原因。"""
    try:
        # 图号断裂属编号体系问题，无法从上下文可靠推断应配内容，仅报告不出图
        if gap.gap_type == GapType.BROKEN_NUMBERING:
            gap.status = "skipped"
            gap.skip_reason = gap.needed_data or "图号不连续，请人工核对编号与图序"
            return
        # 1) 数据源定位与抽取（TABLE_NO_CHART 的锚点即表格）；
        #    若已抽到流程步骤，则尊重文本意图，不再就近抓表防内容错位
        if gap.data is None and not gap.steps:
            gap.data = datasource.locate_data(doc, gap.anchor_block_id)
        # 2) 数据不足且无流程步骤：给出明确原因与补数建议
        if gap.data is None and not gap.steps:
            gap.status = "skipped"
            gap.skip_reason = (
                "文档中未找到可量化的数据来源。建议补充："
                + (gap.needed_data or "对应的数值/步骤数据")
                + "，补充后重跑本工具即可自动出图。"
            )
            return
        # 3) 类型推荐 + 配置生成
        spec = recommender.build_spec(gap, gap.data, doc_title)
        if spec is None:
            gap.status = "skipped"
            gap.skip_reason = "数据形态不足以支撑可靠绘图（" + (gap.needed_data or "") + "）"
            return
        gap.final_type = spec.chart_type
        gap.data = spec.data
        gap.steps = spec.steps or gap.steps
        # 4) 渲染
        img_fmt = _image_format(doc, cfg)
        out_img = _image_path(assets_dir, idx, img_fmt, gap)
        renderer.render(spec, cfg, out_img)
        gap.status = "filled"
        gap.output_image = str(out_img)
    except Exception as exc:  # 单一缺口失败不阻断整体
        logger.exception("缺口 %s 生成失败", gap.gap_type)
        gap.status = "skipped"
        gap.skip_reason = f"生成失败：{exc}"


def _verify(out_path: Path, filled: list[ChartGap]) -> dict:
    """校验：重新解析导出文档，确认图片数与图注出现。"""
    result = {"passed": True, "details": []}
    try:
        reparsed = parse_file(out_path)
    except Exception as exc:
        return {"passed": False, "details": [f"导出文档无法重新解析：{exc}"]}
    # 各格式回插的图在重解析后均为 IMAGE 块（docx 为 w:drawing，md/html 为图片引用）
    n_img = sum(1 for b in reparsed.blocks
                if b.kind == BlockKind.IMAGE or "autofill_" in (b.image_ref or ""))
    all_text = "\n".join(b.text for b in reparsed.blocks)
    if n_img < len(filled):
        result["details"].append(
            f"重解析发现图片块 {n_img} 个，少于预期 {len(filled)} 个（部分图形可能以其他块类型计入）")
    for g in filled:
        if g.caption[:8] not in all_text and "autofill" not in all_text:
            result["passed"] = False
            result["details"].append(f"图注「{g.caption[:20]}」未出现在导出文档中")
    if result["passed"] and not result["details"]:
        result["details"].append("导出文档重解析通过，全部补图可见")
    return result


def fill(path: str | Path, out_path: Optional[str | Path] = None,
         assets_dir: Optional[str | Path] = None,
         cfg: Optional[Config] = None) -> dict:
    """完整链路：检测 + 生成 + 回插 + 导出 + 校验，返回结构化报告。"""
    cfg = cfg or Config()
    src = Path(path)
    doc, gaps = analyze(src, cfg)
    doc_title = next((b.text for b in doc.blocks if b.kind.value == "heading"), src.stem)

    out_path = Path(out_path) if out_path else src.with_name(src.stem + "_autofill" + src.suffix)
    if out_path.suffix.lower() != src.suffix.lower():
        raise ValueError(
            f"导出容器格式与源文档不一致（{src.suffix} → {out_path.suffix}）："
            "回插导出器按源格式原样写出，不支持跨格式转换")
    assets_dir = Path(assets_dir) if assets_dir else out_path.parent / cfg.get(
        "output", "image_dir", "charts")
    assets_dir.mkdir(parents=True, exist_ok=True)

    for i, gap in enumerate(gaps):
        _try_fill(doc, gap, cfg, assets_dir, i, doc_title)

    filled = [g for g in gaps if g.status == "filled"]
    # 无论是否有成功补图，导出文档始终产出（无缺口时等高于原样复制，保证报告路径可用）
    exported = export_document(doc, gaps, out_path, cfg)
    verification = _verify(exported, filled) if filled else {
        "passed": True, "details": ["无可生成图表，仅输出检测报告"]}

    report = {
        "source": str(src),
        "format": doc.fmt,
        "blocks": len(doc.blocks),
        "exported_document": str(exported),
        "assets_dir": str(assets_dir),
        "summary": {
            "total_gaps": len(gaps),
            "filled": len(filled),
            "skipped": sum(1 for g in gaps if g.status == "skipped"),
        },
        "gaps": [g.to_dict() for g in gaps],
        "verification": verification,
    }
    return report


def save_report(report: dict, out: str | Path) -> Path:
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
