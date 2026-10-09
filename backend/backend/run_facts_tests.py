import os
import subprocess
import sys

os.chdir(r"J:\编程\专项方案工具箱\backend")
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

tests = [
    "tests/test_facts_extractor.py",
    "tests/test_facts_classification.py",
    "tests/test_global_facts_persist.py",
    "tests/test_facts_module_fixes_20261001.py",
]
cmd = [sys.executable, "-m", "pytest"] + tests + ["-q", "-p", "no:cacheprovider", "--timeout=120"]
r = subprocess.run(cmd, capture_output=True)
sys.stdout.buffer.write(r.stdout)
sys.stderr.buffer.write(r.stderr)
sys.exit(r.returncode)
