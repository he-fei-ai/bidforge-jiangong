# -*- coding: utf-8 -*-
import sys

files = [
    r"app\routers\compliance.py",
    r"app\routers\export.py",
    r"app\routers\_export_issue_scan.py",
    r"app\routers\scoring.py",
    r"app\services\review_autofix.py",
    r"tests\test_review_leftovers_r198_20261109.py",
    r"tests\test_outline_closeout_r86_20261016.py",
    r"tests\test_outline_closeout_r87_20261016.py",
]
import os
base = r"j:\编程\专项方案工具箱\backend"
ok = True
for f in files:
    b = open(os.path.join(base, f), "rb").read()
    crlf = b.count(b"\r\n")
    lf_only = b.count(b"\n") - crlf
    bom = b.startswith(b"\xef\xbb\xbf")
    fffd = b.decode("utf-8", errors="replace").count("\ufffd")
    s = "OK" if (lf_only == 0 and not bom and fffd == 0) else "WARN"
    if s != "OK":
        ok = False
    print(f"{s} {f} CRLF={crlf} LF_only={lf_only} BOM={bom} FFFD={fffd}")
print("ALL_OK" if ok else "HAS_WARN")
