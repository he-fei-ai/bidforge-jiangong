"""探测 app/ 下 .pyc 字节码（__pycache__）是否可读可反编译。

目标：在源码不可读的情况下，通过字节码只读还原目录生成模块关键函数的
      实现逻辑（dis 模块不需要源码变量名）。
"""
import importlib.util
import io
import os
import sys
import traceback

os.chdir(os.path.dirname(os.path.abspath(__file__)))
L = []


def log(s=""):
    L.append(str(s))


log("python = " + sys.version.split()[0])

candidates = [
    "app/__pycache__/numbering.cpython-314.pyc",
    "app/__pycache__/outline_checkpoint.cpython-314.pyc",
    "app/__pycache__/outline_templates.cpython-314.pyc",
    "app/__pycache__/outline_quality.cpython-314.pyc",
    "app/__pycache__/content_checkpoint.cpython-314.pyc",
    "app/services/__pycache__/numbering.cpython-314.pyc",
    "app/services/__pycache__/outline_checkpoint.cpython-314.pyc",
    "app/services/__pycache__/outline_templates.cpython-314.pyc",
    "app/services/__pycache__/outline_reorganize.cpython-314.pyc",
    "app/services/__pycache__/content_checkpoint.cpython-314.pyc",
    "app/services/__pycache__/content_blocks.cpython-314.pyc",
    "app/services/__pycache__/file_parser.cpython-314.pyc",
    "app/services/__pycache__/numbering.cpython-313.pyc",
]

for rel in candidates:
    if not os.path.exists(rel):
        L.append("MISSING  " + rel)
        continue
    try:
        sz = os.path.getsize(rel)
        with io.open(rel, "rb") as f:
            hdr = f.read(16)
        magic = hdr[:4].hex()
        L.append("INFO     %-58s size=%d magic=%s first16=%s"
                 % (rel, sz, magic, hdr[4:16].hex()))
    except BaseException as e:
        L.append("ERR      %-58s %s: %s" % (rel, type(e).__name__, e))

log("\n尝试用 importlib.util 反编译一个候选模块:")
spec = importlib.util.spec_from_file_location("num_pyc",
                                               "app/services/__pycache__/numbering.cpython-314.pyc")
if spec is not None and spec.origin:
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        L.append("SUCCESS  反编译 app/services/__pycache__/numbering.cpython-314.pyc 成功")
        L.append("  模块对象: %s" % mod)
        for name in ["renumber_outline_nodes", "renumber_section_outline_ids",
                     "stored_id_to_display", "strip_outline_numbering"]:
            if hasattr(mod, name):
                obj = getattr(mod, name)
                L.append("  has %-32s -> %s (code: %s)"
                         % (name, type(obj).__name__,
                            (obj.__code__ if hasattr(obj, "__code__") else "n/a")))
            else:
                L.append("  MISSING %-32s" % name)
    except BaseException as e:
        L.append("FAIL     反编译失败: %s: %s" % (type(e).__name__, e))
        L.append("".join(traceback.format_exception_only(type(sys.exception),
                                                         sys.exception())).strip())
else:
    L.append("SKIP     spec 为 None")

with io.open("_coder_pyc_probe_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("pyc probe done -> %d lines" % len(L))
