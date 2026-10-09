"""Fix broken ACLs on all .py files in tests/ using icacls via Python subprocess.
Python handles Unicode paths correctly when passing to subprocess."""
import os
import pathlib
import subprocess

os.chdir(r"J:\编程\专项方案工具箱\backend")
os.environ["PYTHONUTF8"] = "1"

tests_dir = pathlib.Path("tests")
broken = []
for p in tests_dir.rglob("*.py"):
    try:
        p.read_bytes()
    except PermissionError:
        broken.append(str(p.resolve()))

print(f"Broken files: {len(broken)}")

# Try icacls via Python subprocess (handles Unicode paths)
fixed = 0
failed = 0
for fpath in broken:
    # Reset ACL to inherit from parent
    r = subprocess.run(["icacls", fpath, "/reset"], capture_output=True, text=True)
    if r.returncode != 0:
        # Try granting full control to current user
        username = os.getlogin()
        r2 = subprocess.run(["icacls", fpath, "/grant", f"{username}:F"], capture_output=True, text=True)
        if r2.returncode == 0:
            fixed += 1
        else:
            failed += 1
    else:
        fixed += 1

print(f"Fixed via icacls: {fixed}")
print(f"Failed: {failed}")

# Verify
broken2 = []
for p in tests_dir.rglob("*.py"):
    try:
        p.read_bytes()
    except PermissionError:
        broken2.append(str(p))
print(f"Still broken after fix: {len(broken2)}")
