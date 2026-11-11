#!/usr/bin/env python3
"""Generate frontend TypeScript checkpoint schema from backend source of truth.

Usage:
    cd backend
    python tools/generate_checkpoint_schema.py

This writes frontend/src/types/checkpointSchema.ts.
Run after modifying backend/app/services/checkpoint_schema.py.
"""
import sys
from pathlib import Path

# Make backend importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.checkpoint_schema import generate_typescript_schema

OUT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "checkpointSchema.ts"
OUT.parent.mkdir(parents=True, exist_ok=True)

ts = generate_typescript_schema()
# ✅ R217（2026-11-11 · P2）：Windows 上 Path.write_text 的 newline=None 会把
#    \n 统一转成 \r\n，而 .gitattributes 规定 *.ts eol=lf ⇒ 每次「改白名单 →
#    重新生成契约」都会把本文件写成纯 CRLF，触发前端字节体检红，且与 parity
#    护栏按 LF 的比较分叉。改为显式以 LF 字节写出（生成器是真正常驻的写入口）。
OUT.write_bytes(ts.replace("\r\n", "\n").encode("utf-8"))
print(f"Wrote {OUT} ({len(ts)} chars)")