"""启动前置体检（一次性完成全部后端探测，供 start_all.bat 调用）。

背景（2026-11-16 启动缓慢收口）：旧 start_all.bat 为做前置校验，先后拉起
**6 个独立 Python 解释器**（data 目录 junction 探测 / `import app.main` /
Mermaid 渲染器 / rapidocr / win32com / ocr_capabilities）。每个解释器都要
重新付一次「解释器启动 + 全量库 import」的成本——实测 `import app.main`
在暖缓存下约 2.3s、冷缓存可达 15s+，而它在此前「核心依赖探测」步骤里已被
覆盖，随后 uvicorn 启动时还要**再付一次**。把全部探测合并进**单个**解释器，
可省掉 4~5 次重复的「解释器启动 + 库 import」，显著缩短点击 start_all.bat
到后端真正开始启动的等待。

设计约束：
- 纯 stdout 输出 **ASCII**（旧脚本踩过 GBK 控制台下中文 `-c` 触发
  UnicodeEncodeError 假失败的坑，见 start_all.bat 内注释）。
- 核心依赖（fastapi/uvicorn/aiosqlite/pydantic）缺失 → 退出码 1（阻断启动，
  让 BAT 提示安装）；仅可选特性缺失 → 退出码 0（不阻断，只打印提示）。
- 任何探测自身抛异常都按「该特性不可用」处理，绝不因探测崩溃而阻断启动。
"""

from __future__ import annotations

import sys

# ---- 1. 核心依赖（缺失即阻断）----
# 这四项是 uvicorn 拉起 app.main 的硬前置。此处**真实 import**（而非查
# sys.modules）：既确认可导入，又让它们进入本解释器的 sys.modules，
# 使随后 `import app.main` 不必重复加载这批基础库。
try:
    import aiosqlite  # noqa: F401
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import uvicorn  # noqa: F401

    print("[OK] core deps (fastapi/uvicorn/aiosqlite/pydantic)")
except Exception as exc:  # noqa: BLE001 - 核心依赖缺失必须阻断并上报
    print("[CORE-MISSING] %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)

# ---- 2. app 可导入性（阻断）----
# 只做 import，不拉起服务。失败（缺可选依赖/语法错）→ 退出码 1，BAT 提示装依赖。
# 注意：此处 import 成功后，uvicorn 启动会复用 OS 文件缓存与 .pyc，比旧脚本
# 「preflight 付一次 + uvicorn 再付一次」省一半。
try:
    import app.main  # noqa: F401

    print("[OK] app import ok")
except Exception as exc:  # noqa: BLE001 - 体检脚本：任何 import 失败都要如实上报
    print("[APP-IMPORT-FAIL] %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)

# ---- 3. 可选特性（不阻断）----
# Mermaid 渲染器（PIL v2 内置）：缺失只影响图表渲染，不阻断启动。
try:
    from app.services.ai.mermaid_renderer import _chart_cache  # noqa: F401

    print("[OK] mermaid renderer (PIL v2)")
except Exception as exc:  # noqa: BLE001
    print("[WARN] mermaid renderer unavailable: %s: %s" % (type(exc).__name__, exc))

# OCR 引擎（rapidocr / tesseract）：缺失只影响扫描件识别。
_ocr_ok = False
try:
    import rapidocr_onnxruntime  # noqa: F401

    _ocr_ok = True
    print("[OK] ocr engine: rapidocr")
except Exception:  # noqa: BLE001
    try:
        from app.services.ocr import ocr_capabilities

        caps = ocr_capabilities()
        _ocr_ok = bool(caps.get("tesseract") or caps.get("rapidocr"))
    except Exception as exc:  # noqa: BLE001
        print("[WARN] ocr capability probe failed: %s: %s" % (type(exc).__name__, exc))
        _ocr_ok = False
if not _ocr_ok:
    print("[WARN] no ocr engine (scanned files cannot be read); install requirements-ocr.txt")

# Windows PDF 导出组件（win32com / Word 自动化）：缺失只影响 DOCX->PDF。
try:
    import win32com  # noqa: F401

    print("[OK] windows pdf export (win32com)")
except Exception:  # noqa: BLE001
    print("[WARN] win32com unavailable (DOCX->PDF via Word disabled); install requirements-windows.txt")

sys.exit(0)
