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
OUT.write_text(ts, encoding="utf-8")
print(f"Wrote {OUT} ({len(ts)} chars)")