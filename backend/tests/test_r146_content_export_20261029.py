# -*- coding: utf-8 -*-
"""R146（2026-10-29）· 正文生成 + 导出文档 · 六类标准收口护栏。

本文件锁定本轮三处**新增判据/修复**（全部有后台日志依据，见
``docs/正文生成与导出文档模块R146深度审计_20261029.md``）：

A · 表格缺 GFM 分隔行**确定性自动修复**
  ``content_utils.auto_fix_table_separators`` —— 日志 TOP2 error
  （``sse: 格式契约自检: error/table:表格缺少 GFM 分隔行（| --- |），导出会降级为纯文本``）。
  判据保守：只救「连续表格行、整段一处分隔行都没有、段长 ≥2、首行 ≥2 列」。
  同族缺陷回扫：R145 的「表头前短行」已有分隔行 → 本修复**不得**介入（回归锚）。

B · 契约补两条「只在导出侧可见」的判据
  ① chart-json 载荷类型白名单（与登记侧 ``_ALL_CHART_TYPES`` 单一事实源）；
  ② 表格上方缺表题（日志 TOP1：单次导出 11 条「表格无表名行，自动生成表题」）；
  ③ 图/表编号连续性（用户明确要求「编号连续性校验 / 目录正文图号三者一致」）。

C · 接线与开关
  生成侧（sse_handlers）与导出侧（export._prepare_export）必须调用同一纯函数；
  两侧共用**服务端**开关 ``content_autofix_table_separators``（默认 True，False 时逐字回退）。
  刻意不登记进导出 ``_EXPORT_CONFIG_KEYS``：该键的每个成员都必须在导出 UI 有落点
  （``test_export_closeout::TestFrontendBackendConfigParity``），而本修复是确定性纠错、
  不是逐次可选的版式项。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def _py(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8", errors="replace")


# ===================================================================
# A · 表格缺分隔行自动修复（纯函数）
# ===================================================================

class TestAutoFixTableSeparators:

    def test_missing_separator_is_fixed(self):
        """表头 + 数据行（无分隔行）→ 在表头后插入等列数分隔行。"""
        from app.services.content_utils import auto_fix_table_separators

        src = "表 4-1 抹灰参数\n\n| 部位 | 做法 |\n| 底板 | C35 |\n| 顶板 | C40 |"
        new, log = auto_fix_table_separators(src)
        assert log == [{"line": 3, "cols": 2}], log
        assert "| --- | --- |" in new
        lines = new.split("\n")
        assert lines[2] == "| 部位 | 做法 |"
        assert lines[3] == "| --- | --- |"
        assert lines[4] == "| 底板 | C35 |"
        # 其余行逐字节不变
        assert lines[0] == "表 4-1 抹灰参数"

    def test_idempotent(self):
        """修复后的正文再跑一次：内容不变、日志为空（幂等）。"""
        from app.services.content_utils import auto_fix_table_separators

        src = "| a | b |\n| 1 | 2 |"
        new, log = auto_fix_table_separators(src)
        assert log
        new2, log2 = auto_fix_table_separators(new)
        assert new2 == new and log2 == []

    def test_existing_separator_untouched(self):
        """已有分隔行的规范表逐字节不变（含 R145 标题带形态）。"""
        from app.services.content_utils import auto_fix_table_separators

        for src in (
            "| 检验项目 | 允许偏差 |\n| --- | --- |\n| 立面垂直度 | ≤2mm |",
            # R145 表头前短行（分隔行在 index 2）——本修复不得介入
            "| 项目 | 技术参数 |\n| 项目 | 技术参数 | 执行要求 |\n"
            "| --- | --- | --- |\n| 分层厚度 | 每遍 5~9mm | 单遍 5mm 以上 |",
        ):
            new, log = auto_fix_table_separators(src)
            assert new == src and log == [], f"误改已含分隔行的表: {new!r}"

    def test_fenced_block_protected(self):
        """```markdown 围栏内的「表格缺分隔行」不得被修复（围栏内不解析）。"""
        from app.services.content_utils import auto_fix_table_separators

        src = "```markdown\n| a | b |\n| 1 | 2 |\n```"
        new, log = auto_fix_table_separators(src)
        assert new == src and log == []

    def test_single_row_not_fixed(self):
        """单行表格（散行）不补分隔行（避免把散行变成只有表头的空表）。"""
        from app.services.content_utils import auto_fix_table_separators

        src = "正文\n| a | b |\n正文"
        new, log = auto_fix_table_separators(src)
        assert new == src and log == []

    def test_empty_and_none(self):
        from app.services.content_utils import auto_fix_table_separators

        assert auto_fix_table_separators("") == ("", [])
        assert auto_fix_table_separators(None) == (None, [])

    def test_end_to_end_parses_as_table(self):
        """修复 → 解析：缺分隔行的表成为 table 块，渲染后零 Markdown 残留。"""
        from app.routers.export import _add_table_from_markup
        from app.services.content_blocks import _parse_content_blocks
        from app.services.content_utils import auto_fix_table_separators
        from app.services.export_validate import validate_document
        from docx import Document

        src = "表 2-1 主要材料\n\n| 材料 | 规格 |\n| 水泥 | P.O42.5 |\n| 砂 | 中砂 |"
        fixed, log = auto_fix_table_separators(src)
        assert log
        blocks = _parse_content_blocks(fixed)
        tables = [b for b in blocks if b.get("type") == "table"]
        assert len(tables) == 1, blocks
        doc = Document()
        _add_table_from_markup(doc, tables[0]["lines"])
        report = validate_document(doc)
        assert report["markdown_residue"] == 0, report["samples"]


# ===================================================================
# B · 契约新增判据（chart-json 类型白名单 / 表题缺失 / 编号连续性）
# ===================================================================

class TestContractR146:

    def test_chart_json_unknown_type_is_warning(self):
        """chart-json 载荷类型无法归入 7 类白名单 → warning（成稿会静默少图）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(
            '```chart-json\n{"type": "sankey", "nodes": [], "links": []}\n```')
        assert any(i["category"] == "chart" and i["severity"] == "warning"
                   and "白名单" in i["message"] for i in rep["issues"]), rep["issues"]

    def test_chart_json_valid_type_passes(self):
        """合法类型（labor 结构）不报图表问题（回归锚）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(
            '```chart-json\n{"type": "labor", "categories": ["钢筋工"],'
            ' "phases": ["基础"], "data": [[12]]}\n```')
        assert not any(i["category"] == "chart" for i in rep["issues"]), rep["issues"]

    def test_chart_json_type_whitelist_is_single_source(self):
        """契约白名单必须与登记侧/导出侧集合相等（防口径漂移）。"""
        from app.routers._chart_pipeline import (
            _ALL_CHART_TYPES,
            _JSON_CHART_TYPES,
        )
        from app.services.content_format_contract import CHART_JSON_TYPES

        assert set(CHART_JSON_TYPES) == set(_ALL_CHART_TYPES)
        assert set(CHART_JSON_TYPES) == set(_JSON_CHART_TYPES)
        assert CHART_JSON_TYPES == frozenset(
            {"flowchart", "gantt", "architecture", "labor",
             "comparison", "layout", "timeline"})

    def test_table_without_caption_is_warning(self):
        """表头上方没有表题行 → numbering warning（导出只能按表头拼低质量表题）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("| 部位 | 做法 |\n| --- | --- |\n| 底板 | C35 |")
        assert any(i["category"] == "numbering" and i["severity"] == "warning"
                   and "缺少表题" in i["message"] for i in rep["issues"]), rep["issues"]

    def test_table_with_caption_passes(self):
        """表题 + 空行 + 表格 → 不报「缺少表题」（回归锚）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(
            "表 3-1 抹灰工程主要参数\n\n| 部位 | 做法 |\n| --- | --- |\n| 底板 | C35 |")
        assert not any("缺少表题" in i["message"] for i in rep["issues"]), rep["issues"]

    def test_numbering_gap_is_warning(self):
        """同章图号 1 → 3（断号）→ numbering warning。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("图 2-1 组织机构\n\n图 2-3 施工流程")
        assert any(i["category"] == "numbering" and "不连续" in i["message"]
                   for i in rep["issues"]), rep["issues"]

    def test_numbering_continuous_passes(self):
        """同章图号 1、2 连续 → 不报不连续（回归锚）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("图 2-1 组织机构\n\n图 2-2 施工流程")
        assert not any("不连续" in i["message"] for i in rep["issues"]), rep["issues"]

    def test_continuity_check_ignores_fenced_code(self):
        """围栏内的「图 1-5 …」不参与连续性判定（不是图题）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(
            "图 1-1 组织机构\n\n```text\n图 1-5 示例\n```")
        assert not any("不连续" in i["message"] for i in rep["issues"]), rep["issues"]


# ===================================================================
# C · 接线与配置开关
# ===================================================================

class TestWiring:

    def test_generation_side_calls_autofix(self):
        """生成侧：_persist_section 落库前必须调用 auto_fix_table_separators。"""
        tree = ast.parse(_py("routers/sse_handlers.py"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "auto_fix_table_separators"]
        assert calls, "sse_handlers 未接线表格分隔行自动修复"

    def test_export_side_calls_autofix(self):
        """导出侧：_prepare_export 体检前必须调用同一纯函数。"""
        tree = ast.parse(_py("routers/export.py"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "auto_fix_table_separators"]
        assert calls, "export._prepare_export 未接线表格分隔行自动修复"

    def test_config_flag_default_true(self):
        """开关 content_autofix_table_separators 默认 True（可显式关闭回退）。"""
        from app.config import settings

        assert bool(getattr(settings, "content_autofix_table_separators",
                            True)) is True

    def test_no_literal_whitelist_copies(self):
        """7 类白名单不得在契约模块内以字面量复制（只能引用 services 事实源）。"""
        src = _py("services/content_format_contract.py")
        assert not re.search(
            r'["\']flowchart["\']\s*,\s*["\']gantt["\']', src), \
            "content_format_contract 复制了 7 类白名单字面量"
