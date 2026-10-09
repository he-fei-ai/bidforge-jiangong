"""Find and fix all .py files in tests/ that have broken NTFS ACLs.
Python can't read them directly, but PowerShell's Get-Content can.
Strategy: use PowerShell to read content, delete old file, write new file."""
import os
import pathlib

os.chdir(r"J:\编程\专项方案工具箱\backend")
os.environ["PYTHONUTF8"] = "1"

tests_dir = pathlib.Path("tests")
broken = []
ok = []
for p in tests_dir.rglob("*.py"):
    try:
        p.read_bytes()
        ok.append(p)
    except PermissionError:
        broken.append(p)

print(f"Total .py files: {len(ok) + len(broken)}")
print(f"Readable: {len(ok)}")
print(f"Broken (PermissionError): {len(broken)}")
if broken:
    print("\nBroken files (first 20):")
    for p in broken[:20]:
        print(f"  {p}")
