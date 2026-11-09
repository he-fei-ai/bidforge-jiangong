# -*- coding: utf-8 -*-
"""R199 字节体检：触碰文件行尾/BOM/FFFD"""
import sys

FILES = [
    r"j:\编程\专项方案工具箱\backend\app\routers\_chart_pipeline.py",
    r"j:\编程\专项方案工具箱\backend\app\routers\sse_handlers.py",
    r"j:\编程\专项方案工具箱\backend\app\routers\_sse_content_payloads.py",
    r"j:\编程\专项方案工具箱\backend\app\routers\_export_docx_parts.py",
    r"j:\编程\专项方案工具箱\backend\app\routers\charts.py",
    r"j:\编程\专项方案工具箱\backend\app\db.py",
    r"j:\编程\专项方案工具箱\backend\app\config.py",
    r"j:\编程\专项方案工具箱\backend\app\services\ai\mermaid_renderer.py",
    r"j:\编程\专项方案工具箱\backend\app\services\ai\mermaid_service.py",
]
FRONT = [
    r"j:\编程\专项方案工具箱\frontend\src\utils\contentEvents.ts",
    r"j:\编程\专项方案工具箱\frontend\src\tests\contentEvents.test.ts",
    r"j:\编程\专项方案工具箱\frontend\src\pages\SchemeWorkbenchPage.tsx",
    r"j:\编程\专项方案工具箱\frontend\src\hooks\useContentGeneration.ts",
    r"j:\编程\专项方案工具箱\frontend\src\components\MarkdownRenderer.tsx",
]

def check(fp, expect_crlf):
    b = open(fp, "rb").read()
    crlf = b.count(b"\r\n")
    lf_total = b.count(b"\n")
    bom = b.startswith(b"\xef\xbb\xbf")
    fffd = b.count(b"\xef\xbf\xbd")
    mixed = crlf not in (0, lf_total)
    status = "OK" if (not mixed and fffd == 0 and (not expect_crlf or crlf == lf_total) and (expect_crlf or crlf == 0)) else "BAD"
    kind = "CRLF" if crlf == lf_total else ("LF" if crlf == 0 else "MIXED")
    print(f"{status}  {kind:5s}  BOM={bom}  FFFD={fffd}  {fp.split(chr(92))[-1]}")
    return status == "OK"

ok = all(check(f, True) for f in FILES) and all(check(f, False) for f in FRONT)
print("ALL_OK" if ok else "MISMATCH")
sys.exit(0 if ok else 1)
