"""确认 __pycache__ 目录结构与可读性。"""
import io
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
L = []

for d in ("app/__pycache__", "app/services/__pycache__", "app/services/doc_pipeline/__pycache__"):
    if not os.path.exists(d):
        L.append("MISSING: %s" % d)
        continue
    try:
        items = os.listdir(d)
        L.append("DIR: %s (%d entries)" % (d, len(items)))
        for n in items[:30]:
            L.append("   " + n)
    except BaseException as e:
        L.append("ERR %s: %s: %s" % (d, type(e).__name__, e))

# 尝试读一个 py 文件（已知的 readable 文件）
for rel in ("app/services/facts_patches.py",
            "app/services/doc_pipeline/doc_storage.py",
            "app/services/doc_pipeline/md_structured.py"):
    try:
        with io.open(rel, "rb") as f:
            content = f.read()
        L.append("READ-OK %s (%d B)" % (rel, len(content)))
    except BaseException as e:
        L.append("READ-FAIL %s: %s: %s" % (rel, type(e).__name__, e))

with io.open("_coder_pyc_probe2_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("done -> %d lines" % len(L))
