# -*- coding: utf-8 -*-
"""R68 续 · 跨模块 scheme→project 反查单一事实源收敛（2026-10-11）。

R54 在 global_facts 模块内收口了 9 份副本。本轮收口剩余 4 处：
export ×2 → 委托 resolve_scheme_project_id；placeholder_inventory ×1 +
scheme_fingerprint ×1 → 改用 SCHEME_PROJECT_ID_SQL 常量。
compliance.py 的 3 处查三列（name,type,project_id），不同模式，刻意不收口。
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from app.services.ai import mermaid_common as mc
from app.services import facts_extractor as fe

_AI_DIR = Path(inspect.getsourcefile(mc)).resolve().parent
_APP_DIR = _AI_DIR.parent.parent
_RE_PID = re.compile(r"project_id\s+FROM\s+schemes\s+WHERE\s+id", re.IGNORECASE)


class TestExportConverged:

    def test_no_inline_pid_sql_in_export(self):
        src = (_APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
        assert not _RE_PID.search(src), "export.py 仍有内联 scheme->project SQL"

    def test_export_imports_resolve_scheme_project_id(self):
        src = (_APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
        assert "resolve_scheme_project_id" in src

    def test_export_calls_resolve_at_least_twice(self):
        src = (_APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        n = sum(1 for x in ast.walk(tree)
                if isinstance(x, ast.Call)
                and getattr(x.func, "id", "") == "resolve_scheme_project_id")
        assert n >= 2, f"export.py 调用 resolve_scheme_project_id {n} 次（要求 >=2）"


class TestSubqueriesUseConstant:

    def test_placeholder_inventory_no_inline_pid(self):
        src = (_APP_DIR / "services" / "placeholder_inventory.py").read_text(encoding="utf-8")
        assert not _RE_PID.search(src)

    def test_placeholder_inventory_uses_constant(self):
        src = (_APP_DIR / "services" / "placeholder_inventory.py").read_text(encoding="utf-8")
        assert "SCHEME_PROJECT_ID_SQL" in src

    def test_scheme_fingerprint_no_inline_pid(self):
        src = (_APP_DIR / "services" / "scheme_fingerprint.py").read_text(encoding="utf-8")
        assert not _RE_PID.search(src)

    def test_scheme_fingerprint_uses_constant(self):
        """✅ 2026-10-12 更新：本文件已由「引用 SCHEME_PROJECT_ID_SQL 子查询常量」
        进一步收敛为引用 ``FACT_SCOPE_PREDICATE``（更高一层的单一事实源，
        global_facts / export / compliance / input_coverage 同此）。
        判据意图不变：**作用域判据必须来自 facts_extractor，不得内联**。"""
        src = (_APP_DIR / "services" / "scheme_fingerprint.py").read_text(encoding="utf-8")
        assert "FACT_SCOPE_PREDICATE" in src, (
            "scheme_fingerprint 必须引用 facts_extractor 的作用域常量")

    def test_scheme_fingerprint_passes_real_project_id(self):
        """✅ 2026-10-12（R70 回归）：``FACT_SCOPE_PREDICATE`` 的第二个占位符是
        **project_id**，不是 scheme_id。上一轮收敛沿用了旧子查询写法的
        ``(scheme_id, scheme_id)`` ⇒ 共享半句恒不成立 ⇒ 项目共享事实从指纹里
        整体消失（共享事实变更不触发「结论已过期」）。此处钉住必须反查真值。"""
        src = (_APP_DIR / "services" / "scheme_fingerprint.py").read_text(encoding="utf-8")
        assert "resolve_scheme_project_id(db, scheme_id)" in src
        assert "(scheme_id, scheme_id)" not in src, (
            "第二个参数必须是反查到的 project_id，不得再传 scheme_id")


class TestGlobalSingleSource:

    KNOWN_BROADER = {"routers/compliance.py"}

    def test_all_pid_queries_are_known_or_converged(self):
        """全仓 project_id FROM schemes WHERE id 只允许在 facts_extractor（常量定义）
        或 compliance（已知例外：查三列）。"""
        offenders = []
        for p in _APP_DIR.rglob("*.py"):
            rel = str(p.relative_to(_APP_DIR)).replace("\\", "/")
            if rel == "services/facts_extractor.py":
                continue
            if rel in self.KNOWN_BROADER:
                continue
            if _RE_PID.search(p.read_text(encoding="utf-8")):
                offenders.append(rel)
        assert not offenders, f"仍有内联 scheme->project SQL：{offenders}"

    def test_compliance_uses_broader_query(self):
        """compliance.py 查的是 name, type, project_id 三列（非纯 project_id）。"""
        src = (_APP_DIR / "routers" / "compliance.py").read_text(encoding="utf-8")
        assert "SELECT name, type, project_id FROM schemes WHERE id=?" in src

    def test_constant_defined_in_facts_extractor(self):
        assert hasattr(fe, "SCHEME_PROJECT_ID_SQL")
        assert re.search(r"project_id\s+from\s+schemes", fe.SCHEME_PROJECT_ID_SQL, re.IGNORECASE)


_TOUCHED = ["routers/export.py", "services/placeholder_inventory.py",
            "services/scheme_fingerprint.py"]


class TestByteIntegrity:

    @pytest.mark.parametrize("rel", _TOUCHED)
    def test_no_corruption(self, rel):
        raw = (_APP_DIR / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 有 BOM"
        assert bytes([0xEF, 0xBF, 0xBD]) not in raw, f"{rel} 有 U+FFFD"
        assert b"\x00" not in raw and b"\x07" not in raw and b"\x0c" not in raw

    @pytest.mark.parametrize("rel", _TOUCHED)
    def test_still_compiles(self, rel):
        compile((_APP_DIR / rel).read_text(encoding="utf-8"), rel, "exec")
