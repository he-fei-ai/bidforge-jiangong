import os
import subprocess
import sys
import time

os.chdir(r"D:\wt_facts\backend")
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

all_tests = [
    # Facts module
    "tests/test_facts_extractor.py",
    "tests/test_facts_classification.py",
    "tests/test_global_facts_persist.py",
    "tests/test_facts_module_fixes_20261001.py",
    "tests/test_global_facts_reference_parity_20260930.py",
    "tests/test_global_facts_legacy_closeout_20260930.py",
    "tests/test_facts_content_fixes_20260930.py",
    "tests/test_facts_content_audit_20260929.py",
    "tests/test_facts_deep_audit_r45_20261006.py",
    "tests/test_facts_crosscheck_preserve_20261003.py",
    "tests/test_facts_incremental.py",
    "tests/test_global_facts_routes.py",
    "tests/test_global_facts_field_completeness.py",
    "tests/test_global_facts_enhance_20261003.py",
    # Content module
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
    "tests/test_content_polish_tilde_fences_20260903.py",
    "tests/test_content_standard_service.py",
    # Cross-module
    "tests/test_seven_module_fixes_20260927.py",
    "tests/test_compliance_ai_facts_20261003.py",
    "tests/test_preflight_facts_bridge_20261003.py",
    "tests/test_global_facts_cross_module.py",
    "tests/test_global_facts_safety_scope.py",
    "tests/test_facts_sse_endpoint_no_422_20261003.py",
]

cmd = [sys.executable, "-m", "pytest"] + all_tests + ["-q", "-p", "no:cacheprovider", "--tb=line"]
t0 = time.time()
r = subprocess.run(cmd, capture_output=True, timeout=600)
elapsed = time.time() - t0
out = r.stdout.decode("utf-8", "replace")
err = r.stderr.decode("utf-8", "replace")
print(f"FINAL BASELINE ({elapsed:.1f}s, rc={r.returncode})")
print(f"{'='*60}")
lines = out.strip().split("\n")
for line in lines[-30:]:
    print(line)
if r.returncode != 0 and err.strip():
    print("--- STDERR ---")
    for line in err.strip().split("\n")[-10:]:
        print(line)
