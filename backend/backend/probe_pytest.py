import os
import pathlib
import subprocess
import sys

os.chdir(r"J:\编程\专项方案工具箱\backend")
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

# Check test file readability
for f in ["tests/test_facts_extractor.py", "tests/test_facts_classification.py", 
          "tests/test_content_utils.py", "tests/test_content_runtime.py"]:
    p = pathlib.Path(f)
    print(f"{f}: exists={p.exists()}, size={p.stat().st_size if p.exists() else 'N/A'}")

# Try collecting just one test file
print("\n--- pytest collection test ---")
cmd = [sys.executable, "-m", "pytest", "tests/test_content_utils.py", 
       "--collect-only", "-q", "-p", "no:cacheprovider"]
r = subprocess.run(cmd, capture_output=True, timeout=60)
print("stdout:", r.stdout.decode("utf-8", "replace")[:500])
print("stderr:", r.stderr.decode("utf-8", "replace")[:500])
print("returncode:", r.returncode)
