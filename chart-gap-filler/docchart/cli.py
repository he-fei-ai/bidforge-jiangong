# -*- coding: utf-8 -*-
"""命令行入口：analyze（检测报告）/ fill（检测+补图+导出）。

用法示例：
    python -m docchart.cli analyze 文档.docx
    python -m docchart.cli fill 文档.docx -o 新文档.docx --report report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config
from .parsers import supported_formats
from .pipeline import analyze, fill, save_report


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _print_gaps(gaps, doc) -> None:
    if not gaps:
        print("未检测到缺失图表。")
        return
    print(f"共检测到 {len(gaps)} 处「应配图但缺失」：\n")
    print(f"{'#':>3} | {'置信度':>5} | {'类型':<24} | {'建议图型':<8} | 位置/原因")
    print("-" * 100)
    for i, g in enumerate(gaps, 1):
        loc = g.section or doc.nearest_heading(g.anchor_block_id)
        print(f"{i:>3} | {g.confidence:>5.2f} | {g.gap_type.value:<24} | "
              f"{g.suggested_type or 'auto':<8} | [{loc[:24]}] {g.reason[:60]}")


def cmd_analyze(args) -> int:
    cfg = Config.load(args.config)
    doc, gaps = analyze(args.doc, cfg)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps([g.to_dict() for g in gaps], ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"清单已写入 {args.json}")
    _print_gaps(gaps, doc)
    return 0


def cmd_fill(args) -> int:
    cfg = Config.load(args.config)
    report = fill(args.doc, out_path=args.out, assets_dir=args.assets, cfg=cfg)
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"导出文档: {report['exported_document']}")
    print(f"图片目录: {report['assets_dir']}")
    print(f"校验结果: {'通过' if report['verification']['passed'] else '未通过'}"
          f" - {'; '.join(report['verification']['details'])}")
    for g in report["gaps"]:
        mark = {"filled": "[OK]  ", "skipped": "[SKIP]", "pending": "[----]"}[g["status"]]
        line = f"{mark} {g['gap_type']:<24} {g['caption'][:24]:<26} {g['reason'][:44]}"
        if g["status"] == "skipped":
            line += f"\n       └ 原因/建议: {g['skip_reason'][:90]}"
        print(line)
    if args.report:
        save_report(report, args.report)
        print(f"报告已写入 {args.report}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="docchart",
        description="文档图表缺失检测与自动补全系统（支持："
                    + ", ".join(supported_formats()) + "）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ana = sub.add_parser("analyze", help="只检测缺失图表，输出清单")
    p_ana.add_argument("doc", help="输入文档路径（.md/.html/.docx）")
    p_ana.add_argument("--config", help="配置文件（.yaml/.json）")
    p_ana.add_argument("--json", help="把清单写入 JSON 文件")

    p_fill = sub.add_parser("fill", help="检测 + 生成图表 + 回插导出新文档")
    p_fill.add_argument("doc")
    p_fill.add_argument("-o", "--out", help="导出文档路径（默认原名加 _autofill）")
    p_fill.add_argument("--assets", help="图片输出目录（默认导出文档旁 charts/）")
    p_fill.add_argument("--report", help="把完整 JSON 报告写入文件")
    p_fill.add_argument("--config")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    if args.cmd == "analyze":
        return cmd_analyze(args)
    if args.cmd == "fill":
        return cmd_fill(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
