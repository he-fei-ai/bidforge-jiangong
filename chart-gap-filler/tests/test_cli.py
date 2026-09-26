# -*- coding: utf-8 -*-
"""CLI 组件级交互测试：命令行 -> pipeline -> 报告文件的完整调用链路。"""

import json
from pathlib import Path

from docchart.cli import main


def _sample_md(tmp_path: Path) -> Path:
    src = tmp_path / "doc.md"
    src.write_text(
        "# 工程简报\n\n## 进度\n\n"
        "| 月 | 浇筑量(m³) |\n| --- | --- |\n| 3月 | 120 |\n| 4月 | 260 |\n| 5月 | 310 |\n\n"
        "后续拆除工艺流程如下图所示：\n\n检查平台→拆除连墙件→拆除架体→材料转运\n",
        encoding="utf-8")
    return src


def test_cli_analyze_writes_json(tmp_path: Path):
    gaps_file = tmp_path / "gaps.json"
    rc = main(["analyze", str(_sample_md(tmp_path)), "--json", str(gaps_file)])
    assert rc == 0
    gaps = json.loads(gaps_file.read_text(encoding="utf-8"))
    assert isinstance(gaps, list) and gaps
    assert {"gap_type", "confidence", "anchor_block_id", "section"} <= set(gaps[0])


def test_cli_fill_full_chain(tmp_path: Path):
    src = _sample_md(tmp_path)
    out = tmp_path / "filled.md"
    report_file = tmp_path / "reg.json"
    rc = main(["fill", str(src), "-o", str(out), "--report", str(report_file)])
    assert rc == 0
    assert out.exists()
    report = json.loads(report_file.read_text(encoding="utf-8"))
    # 报告契约：摘要 + 缺口明细 + 校验结论
    assert report["summary"]["filled"] >= 2
    assert report["verification"]["passed"]
    assert {g["status"] for g in report["gaps"]} <= {"filled", "skipped"}


def test_cli_fill_default_out_path(tmp_path: Path, monkeypatch):
    src = _sample_md(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main(["fill", src.name])
    assert rc == 0
    assert (tmp_path / "doc_autofill.md").exists()
