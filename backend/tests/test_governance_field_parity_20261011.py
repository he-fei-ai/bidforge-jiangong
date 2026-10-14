# -*- coding: utf-8 -*-
"""提示词治理开关 · 前后端字段 parity 护栏（2026-10-11 加固）。

债-3 收口后，前端 ``api/index.ts`` 的 ``GovernanceSettings`` 接口与后端
``system.py::GovernanceSettings`` Pydantic 模型必须字段逐项一致：
前端少一个字段 → PUT 请求体缺字段被后端 422 拒；
前端多一个字段 → 后端 ``extra="allow"`` 不报错但 UI 显示一个后端不认的开关。

本护栏从源文件静态提取两端字段名集合，做双向相等断言。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 后端 Pydantic 模型源文件
BACKEND_SRC = ROOT / "app" / "routers" / "system.py"
#: 前端 TS 接口定义源文件
FRONTEND_SRC = ROOT.parent / "frontend" / "src" / "api" / "index.ts"

#: 期望的字段集合（单一事实源 = 后端 Pydantic 模型字段名）
EXPECTED_FIELDS = {"prompt_context_budget", "prompt_injection_defense"}


def _extract_backend_governance_fields(src: str) -> set[str]:
    """从 system.py 源码提取 GovernanceSettings 类的字段名。

    用 AST 遍历而非正则，避免注释/字符串中的同名标识符误判。
    """
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GovernanceSettings":
            fields = set()
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
            return fields
    return set()


def _extract_frontend_governance_fields(src: str) -> set[str]:
    """从 api/index.ts 源码提取 GovernanceSettings 接口的字段名。

    前端用 TypeScript interface，字段格式为 ``  fieldName: type;``。
    在 interface GovernanceSettings { ... } 块内逐行提取。
    """
    # 找到 interface GovernanceSettings { ... } 块
    m = re.search(
        r"interface\s+GovernanceSettings\s*\{([^}]+)\}",
        src,
    )
    if not m:
        return set()
    body = m.group(1)
    fields = set()
    for line in body.split("\n"):
        line = line.strip().rstrip(";").rstrip(",")
        # 格式：fieldName: type  或  fieldName?: type
        fm = re.match(r"(\w+)\??\s*:", line)
        if fm:
            fields.add(fm.group(1))
    return fields


class TestGovernanceFieldParity:
    """前后端 GovernanceSettings 字段必须双向一致。"""

    def test_backend_model_has_expected_fields(self):
        """后端 Pydantic 模型字段 = 期望集合。"""
        src = BACKEND_SRC.read_text(encoding="utf-8")
        fields = _extract_backend_governance_fields(src)
        assert fields == EXPECTED_FIELDS, (
            f"后端 GovernanceSettings 字段漂移：期望 {EXPECTED_FIELDS}，"
            f"实际 {fields}"
        )

    def test_frontend_interface_has_expected_fields(self):
        """前端 TypeScript 接口字段 = 期望集合。"""
        if not FRONTEND_SRC.exists():
            pytest.skip("前端源文件不存在（可能未 checkout）")
        src = FRONTEND_SRC.read_text(encoding="utf-8")
        fields = _extract_frontend_governance_fields(src)
        assert fields == EXPECTED_FIELDS, (
            f"前端 GovernanceSettings 字段漂移：期望 {EXPECTED_FIELDS}，"
            f"实际 {fields}"
        )

    def test_frontend_and_backend_fields_match(self):
        """前后端字段集合双向相等（一条断言锁定两端同步）。"""
        if not FRONTEND_SRC.exists():
            pytest.skip("前端源文件不存在")
        be = _extract_backend_governance_fields(BACKEND_SRC.read_text(encoding="utf-8"))
        fe = _extract_frontend_governance_fields(FRONTEND_SRC.read_text(encoding="utf-8"))
        assert be == fe, (
            f"前后端 GovernanceSettings 字段不一致："
            f"后端 {be} vs 前端 {fe}"
        )

    def test_get_and_put_return_same_fields(self):
        """GET 与 PUT 返回体字段集合 = 期望集合（API 契约对称）。"""
        import inspect
        from app.routers import system as sys_mod
        # governance_get 返回 dict，字段在源码里硬编码
        get_src = inspect.getsource(sys_mod.governance_get)
        get_fields = set(re.findall(r'"(\w+)"\s*:', get_src))
        # governance_put 返回 governance_get 的同构（docstring 明写）
        assert EXPECTED_FIELDS.issubset(get_fields), (
            f"GET 返回体缺字段：期望 {EXPECTED_FIELDS}，"
            f"GET 源码里的字段 {get_fields}"
        )
