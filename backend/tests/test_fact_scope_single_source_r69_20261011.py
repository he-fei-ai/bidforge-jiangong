# -*- coding: utf-8 -*-
"""作用域谓词单一事实源护栏（2026-10-11 · DEBT-3 收口 · R69）。

背景：全局事实「作用域谓词」（判断一行 global_facts 是否属于本方案可见范围）
此前有 3 份各自内联的字面量副本：
  ① ``global_facts.py:527 _FACT_SCOPE_PREDICATE``（全范围）
  ② ``facts_extractor.py:2911 FACT_SCOPE_SHARED_FRAGMENT``（仅共享半句）
  ③ ``export.py:1159`` 内联全范围字面量

R69 收口：在 ``facts_extractor.py`` 新增 ``FACT_SCOPE_PREDICATE``（组合自
``FACT_SCOPE_SHARED_FRAGMENT``），① 改为 import 别名，③ 改为 import 常量。
三份副本 → 单一事实源。

护栏判据锚定 **ast.Constant 字符串字面量**（真正下发给 SQL 的那些字符串），
不锚定源码全文 —— 注释（``#:`` / ``#``）不是 ast.Constant，不会误报；
docstring 是 ast.Constant，若 docstring 里写了完整 SQL 谓词模式也会被抓
（期望行为：防 docstring 藏第二份口径）。沿 §5.14 教训。
"""
import ast
import os
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"

# 两种字面量签名（出现在 SQL WHERE 子句里的核心片段）
FULL_SIG = "scheme_id=? OR (project_id=?"
SHARED_SIG = "project_id=? AND (scheme_id='' OR scheme_id IS NULL"
# ✅ 2026-10-11 · 跨模块数据链 R70：补「半句前缀」变体签名。
#  R69 首版只认 ``scheme_id=? OR (project_id=?`` —— 要求 ``(project_id=`` 后紧跟
#  占位符 ``?``。而 ``services/scheme_fingerprint.py`` 把同一条谓词写成 f-string
#  三段拼接（``"…scheme_id=? OR (project_id=" + f"({SCHEME_PROJECT_ID_SQL}) AND "``），
#  ast.Constant 里落的是 ``scheme_id=? OR (project_id=``（末尾是左括号不是 ``?``），
#  FULL_SIG 完全匹配不到 ⇒ **第四份副本在护栏眼皮下存活**。
#  本签名覆盖「占位符被别的东西替换」的一切写法（子查询 / 常量 / 拼接），
#  与 FULL_SIG 并列扫描，堵住该盲区。
FULL_SIG_ALT = "scheme_id=? OR (project_id="


def _string_constants_in_file(path: Path) -> list[str]:
    """提取 .py 文件中所有字符串字面量的值（含 docstring）。

    判据锚定 ``ast.Constant``（字符串字面量），不锚定源码全文 ——
    注释（``#:`` / ``#``）不是 ast.Constant，不会误报；docstring 是 ast.Constant，
    若 docstring 里写了完整 SQL 谓词模式也会被抓（期望行为：防第二份口径藏进 docstring）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def _all_py_files() -> list[Path]:
    """递归收集 ``app/`` 下所有 .py 文件。"""
    result = []
    for root, _, files in os.walk(APP):
        for fn in files:
            if fn.endswith(".py"):
                result.append(Path(root) / fn)
    return result


class TestFactScopeSingleSource:
    """作用域谓词字面量只允许出现在 ``facts_extractor.py`` 的定义处。"""

    def test_full_range_literal_nowhere_in_app(self):
        """全范围谓词字面量（``scheme_id=? OR (project_id=?``）在全仓
        ``app/`` 下的 ast.Constant 中 **0 命中** —— 所有全范围用法都改为
        常量引用了。"""
        hits = []
        for p in _all_py_files():
            for c in _string_constants_in_file(p):
                if FULL_SIG in c:
                    hits.append((p.relative_to(BACKEND), c[:80]))
        assert hits == [], f"全范围谓词字面量出现在 ast.Constant：{hits}"

    def test_full_range_prefix_variant_nowhere_in_app(self):
        """✅ R70（2026-10-11）：补 ``FULL_SIG`` 的**半句前缀**变体扫描。

        ``FULL_SIG`` 要求 ``(project_id=`` 后紧跟 ``?``，而把谓词写成 f-string
        分段拼接（``scheme_id=? OR (project_id=" + "({子查询}) AND "``）时，
        ast.Constant 落的是 ``scheme_id=? OR (project_id=``，FULL_SIG 匹配不到
        —— ``scheme_fingerprint.py`` 的第四份副本正是靠这个盲区存活。
        本用例用前缀签名堵住：只要出现「方案私有 OR 项目共享」的左半句即红。
        """
        hits = []
        for p in _all_py_files():
            for c in _string_constants_in_file(p):
                if FULL_SIG_ALT in c:
                    hits.append((p.relative_to(BACKEND), c[:80]))
        assert hits == [], f"作用域谓词左半句字面量出现在 ast.Constant：{hits}"

    def test_scheme_fingerprint_uses_shared_predicate(self):
        """``scheme_fingerprint.py`` 必须引用 ``FACT_SCOPE_PREDICATE``，
        不得再内联自己的作用域判据（R70 收敛的第四份副本）。"""
        sf = APP / "services" / "scheme_fingerprint.py"
        src = sf.read_text(encoding="utf-8")
        assert "FACT_SCOPE_PREDICATE" in src, (
            "scheme_fingerprint.py 必须引用 FACT_SCOPE_PREDICATE 单一事实源")
        for c in _string_constants_in_file(sf):
            assert FULL_SIG_ALT not in c, (
                f"scheme_fingerprint.py 仍内联作用域谓词：{c[:80]}")
            assert SHARED_SIG not in c, (
                f"scheme_fingerprint.py 仍内联共享半句：{c[:80]}")

    def test_shared_fragment_literal_only_in_facts_extractor(self):
        """共享半句字面量只允许出现在 ``facts_extractor.py`` 的
        ``FACT_SCOPE_SHARED_FRAGMENT`` 定义处，其它文件 0 命中。"""
        hits = []
        for p in _all_py_files():
            if p.name == "facts_extractor.py" and p.parent.name == "services":
                continue  # 允许定义处
            for c in _string_constants_in_file(p):
                if SHARED_SIG in c:
                    hits.append((p.relative_to(BACKEND), c[:80]))
        assert hits == [], f"共享半句字面量出现在非授权文件：{hits}"

    def test_shared_fragment_defined_exactly_once_in_facts_extractor(self):
        """``FACT_SCOPE_SHARED_FRAGMENT`` 在 ``facts_extractor.py`` 中只定义一次。"""
        fe = APP / "services" / "facts_extractor.py"
        matches = [c for c in _string_constants_in_file(fe) if SHARED_SIG in c]
        assert len(matches) == 1, f"期望 1 处定义，实际 {len(matches)}：{matches}"

    def test_fact_scope_predicate_combines_shared_fragment(self):
        """``FACT_SCOPE_PREDICATE`` 必须组合自 ``FACT_SCOPE_SHARED_FRAGMENT``，
        不得内联全范围字面量（否则就是第二份副本）。"""
        fe = APP / "services" / "facts_extractor.py"
        src = fe.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(fe))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "FACT_SCOPE_PREDICATE":
                        val_seg = ast.get_source_segment(src, node.value) or ""
                        assert "FACT_SCOPE_SHARED_FRAGMENT" in val_seg, (
                            f"FACT_SCOPE_PREDICATE 必须组合自 FACT_SCOPE_SHARED_FRAGMENT，"
                            f"实际：{val_seg}"
                        )
                        assert FULL_SIG not in val_seg, (
                            f"FACT_SCOPE_PREDICATE 不得内联全范围字面量，实际：{val_seg}"
                        )
                        return
        pytest.fail("未找到 FACT_SCOPE_PREDICATE 定义")


class TestImportWiring:
    """import 接线与循环依赖防护。"""

    def test_global_facts_predicate_is_alias(self):
        """``global_facts.py`` 的 ``_FACT_SCOPE_PREDICATE`` 必须是
        ``FACT_SCOPE_PREDICATE`` 的别名（``ast.Name``），不是字面量。"""
        gf = APP / "routers" / "global_facts.py"
        tree = ast.parse(gf.read_text(encoding="utf-8"), filename=str(gf))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "_FACT_SCOPE_PREDICATE":
                        assert isinstance(node.value, ast.Name), (
                            f"_FACT_SCOPE_PREDICATE 必须是 Name（别名），"
                            f"实际：{type(node.value).__name__}"
                        )
                        assert node.value.id == "FACT_SCOPE_PREDICATE", (
                            f"_FACT_SCOPE_PREDICATE 必须指向 FACT_SCOPE_PREDICATE，"
                            f"实际：{node.value.id}"
                        )
                        return
        pytest.fail("未找到 _FACT_SCOPE_PREDICATE 定义")

    def test_global_facts_imports_fact_scope_predicate(self):
        """``global_facts.py`` 的 import 块必须包含 ``FACT_SCOPE_PREDICATE``。"""
        gf = APP / "routers" / "global_facts.py"
        tree = ast.parse(gf.read_text(encoding="utf-8"), filename=str(gf))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "facts_extractor" in node.module:
                for alias in node.names:
                    imported.add(alias.name)
        assert "FACT_SCOPE_PREDICATE" in imported, (
            f"global_facts.py 未 import FACT_SCOPE_PREDICATE，已 import：{imported}"
        )

    def test_export_imports_fact_scope_predicate(self):
        """``export.py`` 必须从 ``facts_extractor`` import ``FACT_SCOPE_PREDICATE``。"""
        ep = APP / "routers" / "export.py"
        tree = ast.parse(ep.read_text(encoding="utf-8"), filename=str(ep))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "facts_extractor" in node.module:
                for alias in node.names:
                    imported.add(alias.name)
        assert "FACT_SCOPE_PREDICATE" in imported, (
            f"export.py 未 import FACT_SCOPE_PREDICATE，已 import：{imported}"
        )

    def test_export_no_inline_scope_literal(self):
        """``export.py`` 的 ast.Constant 中不得出现全范围或共享半句字面量。"""
        ep = APP / "routers" / "export.py"
        consts = _string_constants_in_file(ep)
        full_hits = [c for c in consts if FULL_SIG in c]
        shared_hits = [c for c in consts if SHARED_SIG in c]
        assert full_hits == [], f"export.py 含全范围谓词字面量：{full_hits}"
        assert shared_hits == [], f"export.py 含共享半句字面量：{shared_hits}"

    def test_no_circular_import(self):
        """import 链无循环：``facts_extractor``（services 层）不得 import ``export``（routers 层）。"""
        fe = APP / "services" / "facts_extractor.py"
        src = fe.read_text(encoding="utf-8")
        assert "from app.routers.export" not in src, (
            "facts_extractor.py 不得 import export.py（循环依赖）"
        )


class TestRuntimeEquivalence:
    """运行时验证：三处的谓词常量值逐字一致（单一事实源的终极证明）。"""

    def test_three_sources_produce_same_predicate(self):
        """``facts_extractor`` / ``global_facts`` / ``export`` 三处的
        谓词常量值逐字一致。"""
        from app.routers import export, global_facts
        from app.services import facts_extractor

        fe_val = facts_extractor.FACT_SCOPE_PREDICATE
        gf_val = global_facts._FACT_SCOPE_PREDICATE
        assert hasattr(export, "FACT_SCOPE_PREDICATE"), (
            "export.py 未在模块级暴露 FACT_SCOPE_PREDICATE"
        )
        ep_val = export.FACT_SCOPE_PREDICATE
        assert fe_val == gf_val == ep_val, (
            f"三处谓词值不一致：fe={fe_val!r} gf={gf_val!r} ep={ep_val!r}"
        )

    def test_predicate_has_correct_shape(self):
        """运行时验证：谓词值含 ``scheme_id=?`` / ``project_id=?`` / ``OR``，
        且外层括号闭合（SQL 语义正确）。"""
        from app.services import facts_extractor
        val = facts_extractor.FACT_SCOPE_PREDICATE
        assert "scheme_id=?" in val, f"缺 scheme_id=?：{val}"
        assert "project_id=?" in val, f"缺 project_id=?：{val}"
        assert " OR " in val.upper(), f"缺 OR：{val}"
        assert val.count("(") == val.count(")"), f"括号不配对：{val}"

    def test_shared_fragment_is_substring_of_full(self):
        """运行时验证：共享半句是全范围谓词的子串
        （证明全范围 = ``scheme_id=? OR`` + 共享半句）。"""
        from app.services import facts_extractor
        shared = facts_extractor.FACT_SCOPE_SHARED_FRAGMENT
        full = facts_extractor.FACT_SCOPE_PREDICATE
        assert shared in full, (
            f"共享半句不是全范围谓词的子串：\n  shared={shared!r}\n  full={full!r}"
        )
