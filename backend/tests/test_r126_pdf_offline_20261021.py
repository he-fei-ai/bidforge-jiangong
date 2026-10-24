"""R126（2026-10-21）· E 项：PDF 导出功能整体下线 反向锚护栏。

背景（用户指令「导出只保留导出DOCX格式文档，删除且不再导出PDF格式功能」）：
  · 后端删除 export_pdf 路由 / _convert_docx_to_pdf / _PDF_MIME（export.py）；
  · 前端删除「导出 PDF」按钮与 pdf 阶段枚举（SchemeWorkbenchPage.tsx，
    前端反向锚见 src/tests/exportResponse.test.ts「pdf 阶段不得再现」）；
  · 旧调用方对 POST /schemes/{id}/export/pdf 收 404（用户裁定：彻底删除，
    不留 410 过渡）；存量 fmt=pdf 缓存行不主动清理、由既有 prune 自然淘汰。

本文件两职责：
  A. 反向锚——防 PDF 链死灰复燃：路由/函数/符号/fmt 后缀任何一处重现即红
     （A/B 变异「恢复 @router.post("/pdf")」必须被 1️⃣2️⃣ 抓住）。
  B. 迁移锚——收编原 test_export_pdf_cache_d2_20261001.py 中与 PDF 无关的
     DOCX 同源断言（指纹无 fmt 后缀 / 守卫早于 prepare / 模块可解析），
     原文件整删后这三条检测力由本文件承接，只增不减。

测试策略：AST/源码静态断言 + router.routes 动态清点，零 AI、零 DB。
"""

import ast
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EX_PATH = os.path.join(BACKEND, "app", "routers", "export.py")


def _src(path=EX_PATH):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _fn(name):
    """取出指定顶层 async 函数的源码（找不到返回空串，不抛错——
    PDF 函数已下线，断言方向是「不得存在」）。"""
    src = _src()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


# ---------------------------------------------------------------------------
# A. 反向锚：PDF 导出链不得再现
# ---------------------------------------------------------------------------

def test_r126_pdf_route_not_registered():
    """① 动态清点 router.routes：任何 path 含 /pdf 即红（死灰复燃主锚）。"""
    from app.routers.export import router
    pdf_paths = [r.path for r in router.routes
                 if "pdf" in os.path.basename(r.path).lower()]
    assert not pdf_paths, f"PDF 导出路由重现（R126 已整体下线）: {pdf_paths}"


def test_r126_pdf_functions_removed():
    """② export.py 中 export_pdf / _convert_docx_to_pdf 函数体不得存在。"""
    assert _fn("export_pdf") == "", "export_pdf 函数复活（R126 已整体下线）"
    assert _fn("_convert_docx_to_pdf") == "", "_convert_docx_to_pdf 函数复活"


def test_r126_pdf_symbols_removed():
    """③ _PDF_MIME 赋值与 fmt=pdf 指纹后缀不得再现源码。"""
    src = _src()
    assert '_PDF_MIME = "' not in src, "_PDF_MIME 赋值复活"
    assert '"|fmt=pdf"' not in src and "'|fmt=pdf'" not in src, (
        "fmt=pdf 指纹后缀复活——PDF 缓存键写法回归会重新制造孤儿缓存行")


def test_r126_no_docx_to_pdf_conversion_call():
    """④ 全模块不得再有对转换函数的 await 调用点（调用与定义同体删除；
    若只恢复调用不恢复函数，导入期即 NameError，源码锚另红一道）。"""
    src = _src()
    assert "await _convert_docx_to_pdf(" not in src, (
        "DOCX→PDF 转换调用点复活（R126 已整体下线）")


# ---------------------------------------------------------------------------
# B. 迁移锚：原 D-2 文件中的 DOCX 同源断言（检测力只增不减）
# ---------------------------------------------------------------------------

def test_r126_docx_fingerprint_has_no_format_suffix():
    """迁移自 test_d2_docx_fingerprint_unchanged：DOCX 指纹不得引入 fmt 后缀，
    否则既有缓存全部失效（零失效契约）。"""
    src = _fn("export_docx")
    assert src, "export_docx 必须存在（DOCX 是下线后唯一导出链路）"
    assert '"|fmt=' not in src, "DOCX 指纹被加格式后缀，会导致既有缓存全部失效"
    assert "config_hash, content_hash = _content_fingerprint(prep)" in src


def test_r126_docx_guard_precedence_intact():
    """迁移自 test_d2_docx_guard_precedence_intact：编号一致性守卫必须早于
    _prepare_export（按 await 调用点定位，不搜函数名——防误命中说明文案）。"""
    src = _fn("export_docx")
    i_guard = src.find("await _guard_numbering_consistency(")
    i_prep = src.find("await _prepare_export(")
    assert i_guard > 0 and i_prep > 0
    assert i_guard < i_prep, "守卫顺序回退：必须先于取数准备"


def test_r126_export_module_parses():
    """迁移自 test_export_module_parses：切除 ~350 行后模块必须可解析。"""
    ast.parse(_src(), filename=EX_PATH)
