import os
import subprocess
import sys

os.chdir(r"J:\编程\专项方案工具箱\backend")
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

# Run a single quick test file
cmd = [sys.executable, "-m", "pytest", "tests/test_content_utils.py", 
       "-q", "-p", "no:cacheprovider",
       "--co", "--no-header"]
r = subprocess.run(cmd, capture_output=True, timeout=120)
print("=== COLLECTION ===")
print(r.stdout.decode("utf-8", "replace")[:2000])
print("=== STDERR ===")
print(r.stderr.decode("utf-8", "replace")[:2000])
print("=== RC:", r.returncode)
