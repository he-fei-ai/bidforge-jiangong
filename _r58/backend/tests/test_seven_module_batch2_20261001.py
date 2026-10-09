"""七模块专项（2026-10-01）第二轮修复回归护栏。

覆盖：
  A-1  18 项提取「多文档合计」字符预算默认 30000 → 400000（与解析/落库三级对齐）
       + 修正注释与实现分叉（注释原称「单份文档」，实现是合计且耗尽即整份跳过）
  A-4  global_facts 两处完全静默的 `except Exception: pass` → 补日志与堆栈
  A-9  高频失败点（分段提取异常 / PDF 主通道失败）补 exc_info
  D-1  export_docx 编号守卫前置到 _prepare_export 之前（避免已付 AI 生图成本被作废）

测试策略：配置断言 + 源码/AST 静态断言，零 AI、零 DB。
"""

import ast
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG_PATH = os.path.join(BACKEND, "app", "config.py")
BA_PATH = os.path.join(BACKEND, "app", "routers", "bid_analysis.py")
GF_PATH = os.path.join(BACKEND, "app", "routers", "global_facts.py")
FP_PATH = os.path.join(BACKEND, "app", "services", "file_parser.py")
EX_PATH = os.path.join(BACKEND, "app", "routers", "export.py")


def _src(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# A-1：提取预算与解析/落库三级对齐
# ---------------------------------------------------------------------------

def test_a1_config_default_raised():
    """默认预算必须 ≥ 400000（与 MAX_PARSED_CHARS 对齐）。"""
    from app.config import Settings
    s = Settings()
    assert s.bid_analysis_segment_budget >= 400000, (
        "18 项提取预算仍低于解析能力，中后段文档会被整份跳过（A-1 回退）")


def test_a1_module_fallback_matches_config():
    """bid_analysis 的兜底默认值必须与配置默认一致。"""
    src = _src(BA_PATH)
    assert "bid_analysis_segment_budget" in src
    assert "or 400000" in src, "模块级兜底未同步为 400000（A-1 回退）"


def test_a1_comment_no_longer_says_single_document():
    """注释不得再宣称「单份文档」（实现是多文档合计）。"""
    src = _src(BA_PATH)
    head = src.split("async def _project_document_cols", 1)[0]
    assert "单份文档参与" not in head, "注释仍称单份文档，与实际合计语义不符"


def test_a1_aggregate_semantics_documented():
    """实现仍是合计语义（break 丢弃剩余文档），且已被注释显式说明。"""
    src = _src(BA_PATH)
    assert "headroom = max(0, _MAX_DOC_CHARS - total_chars)" in src
    assert "多文档合计" in src, "合计语义需在注释中显式说明"


def test_a1_budget_ge_parsed_chars_cap():
    """预算不应低于入库上限（MAX_PARSED_CHARS=400000），否则解析成果被二次浪费。"""
    gf = _src(GF_PATH)
    assert "MAX_PARSED_CHARS = 400_000" in gf or "MAX_PARSED_CHARS = 400000" in gf
    from app.config import Settings
    assert Settings().bid_analysis_segment_budget >= 400000


# ---------------------------------------------------------------------------
# A-4：消除静默 except: pass
# ---------------------------------------------------------------------------

def test_a4_parse_status_recheck_no_longer_silent():
    src = _src(GF_PATH)
    assert "复核文档 %s 解析状态失败" in src, (
        "批量解析复核路径仍为静默 except pass（A-4 回退）")


def test_a4_safety_col_probe_no_longer_silent():
    src = _src(GF_PATH)
    assert "探测 global_facts.is_safety_critical 列失败" in src, (
        "安全列探测仍为静默 except pass（A-4 回退）")


def test_a4_recheck_logs_with_traceback():
    """复核失败日志必须带 exc_info（否则仍无法定位 DB 瞬时故障）。"""
    src = _src(GF_PATH)
    i = src.find("复核文档 %s 解析状态失败")
    assert i > 0
    tail = src[i:i + 400]
    assert "exc_info=True" in tail


# ---------------------------------------------------------------------------
# A-9：高频失败点保留堆栈
# ---------------------------------------------------------------------------

def test_a9_segment_exception_has_traceback():
    src = _src(BA_PATH)
    assert "分段 %d 提取异常" in src
    assert 'logger.warning("分段 %d 提取异常: %s", i, pr, exc_info=pr)' in src, (
        "分段提取异常仍无堆栈（A-9 回退）")


def test_a9_pymupdf_failure_has_traceback():
    src = _src(FP_PATH)
    i = src.find("PyMuPDF 解析失败")
    assert i > 0, "未找到 PyMuPDF 失败日志点"
    tail = src[i:i + 300]
    assert "exc_info=True" in tail, "PDF 主通道失败仍无堆栈（A-9 回退）"


# ---------------------------------------------------------------------------
# D-1：export_docx 编号守卫必须早于 _prepare_export
# ---------------------------------------------------------------------------

def _first_call_line(src, funcname):
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == funcname:
                hits.append(node.lineno)
    return min(hits) if hits else None


def test_d1_guard_precedes_prepare_in_source():
    """源码顺序：守卫调用行号 < prepare 调用行号（export_docx 内）。"""
    src = _src(EX_PATH)
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "export_docx":
            fn = node
    assert fn is not None, "未找到 export_docx"
    guard_line = None
    prep_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_guard_numbering_consistency" and guard_line is None:
                guard_line = node.lineno
            if node.func.id == "_prepare_export" and prep_line is None:
                prep_line = node.lineno
    assert guard_line is not None, "export_docx 缺少编号守卫"
    assert prep_line is not None, "export_docx 缺少 _prepare_export"
    assert guard_line < prep_line, (
        f"编号守卫({guard_line}) 必须早于 _prepare_export({prep_line})，"
        "否则严格模式 409 时 AI 生图已计费（D-1 回退）")


def test_d1_pdf_also_guarded_before_prepare():
    """PDF 链保持「守卫 → prepare」顺序（与 DOCX 对齐后的共同口径）。"""
    src = _src(EX_PATH)
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "export_pdf":
            fn = node
    assert fn is not None
    guard_line = prep_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_guard_numbering_consistency" and guard_line is None:
                guard_line = node.lineno
            if node.func.id == "_prepare_export" and prep_line is None:
                prep_line = node.lineno
    assert guard_line is not None and prep_line is not None
    assert guard_line < prep_line


# ---------------------------------------------------------------------------
# 结构性完整性
# ---------------------------------------------------------------------------

def test_all_changed_files_parse():
    for p in (CFG_PATH, BA_PATH, GF_PATH, FP_PATH, EX_PATH):
        ast.parse(_src(p), filename=p)
