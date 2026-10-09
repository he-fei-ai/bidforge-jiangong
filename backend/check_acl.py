import os
import pathlib

os.chdir(r"D:\wt_facts\backend")
ok = 0
bad = 0
for p in pathlib.Path("tests").rglob("*.py"):
    try:
        pathlib.Path(str(p)).read_bytes()
        ok += 1
    except PermissionError:
        bad += 1
print(f"tests: ok={ok} bad={bad}")

ok2 = 0
bad2 = 0
for p in pathlib.Path("app").rglob("*.py"):
    try:
        pathlib.Path(str(p)).read_bytes()
        ok2 += 1
    except PermissionError:
        bad2 += 1
print(f"app: ok={ok2} bad={bad2}")
