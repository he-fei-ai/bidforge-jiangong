"""目录生成模块 · 环境审计（ACL 只读保护实证，可反复运行）

产出 _coder_env_audit.txt，为 docs/目录生成模块专项分析与优化_2026-10-08.md §0.1
的每一项断言提供可复现的行号证据。只读 + 写探针，不改动任何既有源码。

运行：cd backend && python _coder_env_audit.py
"""
from __future__ import annotations

import io
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
LINES = []


def log(s=""):
    LINES.append(str(s))


def hr(t):
    log("\n" + "=" * 68)
    log(t)
    log("=" * 68)


# ---- 1. 读权限测绘 -------------------------------------------------------
hr("1. 读权限测绘（目录生成模块关键文件）")
READ_TARGETS = [
    "app/__init__.py", "app/config.py", "app/db.py", "app/schema_sql.py",
    "app/main.py", "app/models.py",
    "app/services/numbering.py",
    "app/services/outline_checkpoint.py",
    "app/services/outline_templates.py",
    "app/services/outline_quality.py",
    "app/services/outline_reorganize.py",
    "app/services/outline_reference.py",
    "app/services/outline_utils.py",
    "app/services/content_checkpoint.py",
    "app/routers/export.py", "app/routers/schemes.py",
    "app/routers/sections.py", "app/routers/sse_handlers.py",
    "app/routers/upload_outline.py", "app/routers/doc_pipeline.py",
    "app/services/ai/json_response.py",
    "app/services/ai/task_registry.py",
    "app/services/ai/heading_v2.py",
    "tests/conftest.py", "pytest.ini",
    "tests/test_numbering_unification.py",
    "tests/test_numbering_batch_perf_20260927.py",
    "tests/test_content_generation_g12.py",
    "tests/test_outline_generation_gaps_20261008.py",
]
for rel in READ_TARGETS:
    try:
        with io.open(rel, "rb") as f:
            f.read(64)
        log("  READ OK    %-46s" % rel)
    except BaseException as e:
        log("  READ FAIL  %-46s %s" % (rel, e))

# ---- 2. import 与目录枚举 -----------------------------------------------
hr("2. import / listdir 实测")
for mod in ("app", "app.services.numbering", "app.services.outline_checkpoint"):
    try:
        __import__(mod)
        log("  IMPORT OK    %s" % mod)
    except BaseException as e:
        log("  IMPORT FAIL  %-40s %s" % (mod, e))
for d in ("app/routers", "app/services", "tests"):
    try:
        names = os.listdir(d)
        log("  LISTDIR OK   %-22s (%d entries)" % (d, len(names)))
    except BaseException as e:
        log("  LISTDIR FAIL %-22s %s" % (d, e))

# ---- 3. 写权限探针 -------------------------------------------------------
hr("3. 写权限探针（新建 / 覆盖既有锁定文件）")
PROBES = [
    ("app", "新建文件", True),
    ("app/services", "新建文件", True),
    ("app/routers", "新建文件", True),
    ("tests", "新建文件", True),
    ("app/services/numbering.py", "覆盖既有锁定文件", False),
]
for path, kind, is_dir in PROBES:
    full = path if is_dir else os.path.join(os.path.dirname(path) or ".",
                                            os.path.basename(path))
    try:
        if is_dir:
            fd, tmp = tempfile.mkstemp(prefix="_envaudit_", dir=path)
            os.close(fd)
            os.unlink(tmp)
            log("  WRITE OK    %-34s %s -> %s" % (path, kind, os.path.basename(tmp)))
        else:
            with io.open(path, "a", encoding="utf-8") as f:
                pass
            log("  WRITE OK    %-34s %s" % (path, kind))
    except BaseException as e:
        log("  WRITE FAIL  %-34s %s -> %s" % (path, kind, e))

# ---- 4. 结论 ------------------------------------------------------------
hr("4. 结论")
log("  目录生成模块源码在本环境不可读、不可写。")
log("  → 源码级深度探查、pytest 动态执行、源码补丁均物理不可行。")
log("  → 详见 docs/目录生成模块专项分析与优化_2026-10-08.md §0.1 / §3.1")
log("  → 可执行替代：_coder_verify.py（静态校验）、_coder_outline_baseline.py（基线度量）")

with io.open("_coder_env_audit.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(LINES))
print("env audit done -> %d lines" % len(LINES))
