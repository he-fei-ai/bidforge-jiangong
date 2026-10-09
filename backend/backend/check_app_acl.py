import os
import pathlib

os.chdir(r"J:\编程\专项方案工具箱\backend")
ok = 0
bad = 0
for p in pathlib.Path("app").rglob("*.py"):
    try:
        pathlib.Path(str(p)).read_bytes()
        ok += 1
    except PermissionError:
        bad += 1
print(f"app/: ok={ok} bad={bad}")
