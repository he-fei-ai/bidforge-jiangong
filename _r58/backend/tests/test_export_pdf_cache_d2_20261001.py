"""D-2（2026-10-01）：PDF 导出链路缓存 + 坏缓存守卫 回归护栏。

背景：旧实现 export_pdf 完全不读不写 export_cache，也没有坏缓存守卫：
  · 每次导出都重跑最昂贵的 PDF 转换（Word COM / LibreOffice），无秒级命中；
  · 缺图产物（图表渲染失败 / AI 配图未生成 / 位图下载失败）无任何系统侧信号
    （对比 DOCX 的 X-Cache-Status: degraded）。

本轮改动要点（必须被护栏锁定，防止回退）：
  1. PDF 指纹必须带 `|fmt=pdf` 后缀 —— 否则与 DOCX 共用
     (scheme_id, config_hash, content_fingerprint) 唯一键互相覆盖，
     且 INSERT OR IGNORE 会让 PDF 永远写不进缓存。
  2. DOCX 的指纹计算方式不得改变（零缓存失效，用户无需重新导出）。
  3. 缓存命中路径存在且带 X-Cache-Status: hit。
  4. 坏缓存守卫：degraded 时不写缓存，并回传 X-Cache-Status: degraded。

测试策略：AST/源码静态断言 + 纯逻辑，零 AI、零 DB、不触发真实 PDF 转换。
"""

import ast
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EX_PATH = os.path.join(BACKEND, "app", "routers", "export.py")


def _src(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _fn(name):
    """取出指定顶层 async 函数的源码。"""
    src = _src(EX_PATH)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("未找到函数 " + name)


def test_d2_pdf_fingerprint_has_format_suffix():
    src = _fn("export_pdf")
    assert 'content_hash + "|fmt=pdf"' in src, (
        "PDF 缓存键缺格式后缀，会与 DOCX 共用同一唯一键互相覆盖（D-2 回退）")


def test_d2_docx_fingerprint_unchanged():
    """DOCX 仍用原始 content_hash（不引入格式后缀）→ 既有缓存零失效。"""
    src = _fn("export_docx")
    assert '"|fmt=' not in src, "DOCX 指纹被改动，会导致既有缓存全部失效"
    assert "config_hash, content_hash = _content_fingerprint(prep)" in src


def test_d2_pdf_reads_cache_before_conversion():
    """缓存查询必须早于 PDF 转换（否则缓存毫无意义）。"""
    src = _fn("export_pdf")
    i_read = src.find("SELECT result_path FROM export_cache")
    i_conv = src.find("_convert_docx_to_pdf")
    assert i_read > 0, "PDF 链路未查询 export_cache（D-2 回退）"
    assert i_conv > 0, "未找到 PDF 转换调用"
    assert i_read < i_conv, "缓存查询必须在 PDF 转换之前"


def test_d2_cache_hit_returns_status_hit():
    src = _fn("export_pdf")
    assert '"X-Cache-Status": "hit"' in src, "缓存命中未回传 X-Cache-Status: hit"


def test_d2_degraded_guard_present():
    """缺图时不得写缓存（否则服务恢复后永远命中残缺 PDF）。"""
    src = _fn("export_pdf")
    assert "degraded" in src
    # 守卫三条件与 DOCX 同口径
    for cond in ("render_stats.get(\"failed\", 0) > 0",
                 "prep.get(\"ai_image_pending\", 0) > 0",
                 "prep.get(\"ai_image_download_pending\", 0) > 0"):
        assert cond in src, f"坏缓存守卫缺条件: {cond}"
    assert "if not degraded:" in src, "degraded 时仍会写缓存（D-2 回退）"


def test_d2_degraded_returns_status_header():
    src = _fn("export_pdf")
    assert '"degraded" if degraded else "miss"' in src


def test_d2_cache_write_failure_does_not_break_delivery():
    """缓存写入失败必须被兜住，用户仍拿到完整 PDF。"""
    src = _fn("export_pdf")
    assert "PDF 导出缓存写入失败" in src
    assert "exc_info=True" in src


def test_d2_pdf_persists_to_exports_dir():
    """缓存行记路径，产物必须落在 EXPORTS_DIR（临时目录会被清理）。"""
    src = _fn("export_pdf")
    assert "EXPORTS_DIR /" in src
    assert ".pdf" in src


def test_d2_guard_still_before_prepare():
    """D-1 的顺序约束不得被 D-2 破坏：守卫仍早于 _prepare_export。

    注意：必须按 **await 调用点** 定位，不能只搜函数名 —— export_pdf 的
    docstring 里也出现了 `_prepare_export` 字样（说明文案），只搜名字会误命中
    文档字符串而得出错误顺序。
    """
    src = _fn("export_pdf")
    i_guard = src.find("await _guard_numbering_consistency(")
    i_prep = src.find("await _prepare_export(")
    assert i_guard > 0, "未找到守卫调用点"
    assert i_prep > 0, "未找到 prepare 调用点"
    assert i_guard < i_prep


def test_d2_docx_guard_precedence_intact():
    src = _fn("export_docx")
    i_guard = src.find("await _guard_numbering_consistency(")
    i_prep = src.find("await _prepare_export(")
    assert i_guard > 0 and i_prep > 0
    assert i_guard < i_prep


def test_export_module_parses():
    ast.parse(_src(EX_PATH), filename=EX_PATH)
