"""【R126 墓碑（2026-10-21）】原「D-2 PDF 导出链路缓存 + 坏缓存守卫」护栏。

PDF 导出功能已按用户指令整体下线（「导出只保留导出DOCX格式文档」），
export_pdf / _convert_docx_to_pdf / _PDF_MIME 已从 export.py 切除，
本文件原 9 条 PDF 专属用例整体失效、7 条中检测力可迁移的部分已收编至
tests/test_r126_pdf_offline_20261021.py：
  · test_r126_docx_fingerprint_has_no_format_suffix（原 test_d2_docx_fingerprint_unchanged）
  · test_r126_docx_guard_precedence_intact（原 test_d2_docx_guard_precedence_intact）
  · test_r126_export_module_parses（原 test_export_module_parses）
  · 另新增 4 条 PDF 死灰复燃反向锚（路由/函数/符号/调用点）。

本文件刻意保留为零用例墓碑（宿主机 safe-delete 守卫拦截物理删除，
见 AGENTS.md 操作规程；pytest 收集 0 项，等效移除）。
"""
