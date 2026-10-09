import os
import subprocess
import sys

os.chdir(r"D:\wt_facts\backend")
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

# Run facts module tests
facts_tests = [
    "tests/test_facts_extractor.py",
    "tests/test_facts_classification.py",
    "tests/test_global_facts_persist.py",
    "tests/test_facts_module_fixes_20261001.py",
    "tests/test_global_facts_reference_parity_20260930.py",
    "tests/test_global_facts_legacy_closeout_20260930.py",
    "tests/test_facts_content_fixes_20260930.py",
    "tests/test_facts_derivation_closeout_r54_20261008.py",
    "tests/test_facts_deep_audit_r45_20261006.py",
    "tests/test_facts_content_audit_20260929.py",
]

# Run content module tests
content_tests = [
    "tests/test_content_utils.py",
    "tests/test_content_runtime.py",
    "tests/test_content_fence_contract_20260929.py",
    "tests/test_content_checkpoint_closure_20261002.py",
    "tests/test_content_checkpoint_prepend_20261002.py",
    "tests/test_content_outline_enhancement_20261003.py",
    "tests/test_content_data_contract_r52_20261007.py",
    "tests/test_content_quality.py",
    "tests/test_content_progress.py",
    "tests/test_content_shrink.py",
    "tests/test_content_rewrite.py",
    "tests/test_content_terminal_payload_20261005.py",
]

import time


def run_tests(tests, label):
    cmd = [sys.executable, "-m", "pytest"] + tests + ["-q", "-p", "no:cacheprovider", "--tb=short"]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    elapsed = time.time() - t0
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    print(f"\n{'='*60}")
    print(f"{label} ({elapsed:.1f}s, rc={r.returncode})")
    print(f"{'='*60}")
    # Print last 30 lines of stdout
    lines = out.strip().split("\n")
    for line in lines[-30:]:
        print(line)
    if err.strip():
        print("--- STDERR (last 10 lines) ---")
        err_lines = err.strip().split("\n")
        for line in err_lines[-10:]:
            print(line)

run_tests(facts_tests, "FACTS MODULE BASELINE")
run_tests(content_tests, "CONTENT MODULE BASELINE")
