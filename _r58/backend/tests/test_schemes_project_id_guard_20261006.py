"""schemes.project_id 写路径守卫护栏（R45 · 2026-10-06 · G3 根因加固）

背景：``schemes.project_id`` 是 ``NOT NULL`` 但**允许空串**（无 CHECK 约束）。
空串会让按项目维度的缓存失效退化成「双空作用域」的静默 no-op ——
``global_facts._invalidate_fact_scope_cache(db, "", "")`` 直接 return：
已确认的事实改动**一个缓存都不失效**、``schemes.facts_updated_at`` 不推进，
「事实已变更」标记永久停在旧值。

R45 已把 global_facts 侧的失效作用域改为「无条件取方案行的 project_id、
取不到降级按方案级」，本文件加固的是**上游**：让空串根本写不进去。

护栏口径（对齐 R45 · D1 的「单一事实源」模式）：

  ① ``require_project_id`` 语义正确（空/空白/None → 422，正常值 → strip）；
  ② 两条 schemes.py 写路径 + 一条 bid_analysis.py 写路径都调用它；
  ③ **全仓扫描**：任何写 ``schemes.project_id`` 的 SQL 语句，其所在函数必须
     调用 ``require_project_id``（新增第 4 条写路径不接守卫即失败）；
  ④ 动态 SET 路径（``UPDATE schemes SET {sets}``）的字段来源模型
     ``SchemeUpdate`` 不得含 ``project_id``。

⚠️ 生产库实测（2026-10-06）：5 个方案 / 0 空 project_id / 0 孤儿行 ——
空串当前不可达，属**纵深防御**加固，不是修数据。
"""
from __future__ import annotations

import ast
import inspect
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(APP))
sys.path.insert(0, APP)

import pytest  # noqa: E402


# ---------------------------------------------------------------- 测试基建 --
def _module_src(name: str) -> str:
    import importlib
    return inspect.getsource(importlib.import_module(name))


def _func_src(module_name: str, func_name: str) -> str:
    import importlib
    return inspect.getsource(getattr(importlib.import_module(module_name),
                                     func_name))


def _iter_app_py() -> list[str]:
    """列出 app 下全部 .py 文件（护栏扫描范围）。"""
    root = os.path.join(APP, "app")
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith((".", "__"))]
        out.extend(os.path.join(dirpath, f) for f in filenames
                   if f.endswith(".py"))
    return out


def path_to_module(path: str) -> str:
    """绝对路径 → 模块名（app/routers/x.py → app.routers.x）。"""
    rel = os.path.relpath(path, APP)
    rel = rel[:-3] if rel.endswith(".py") else rel
    return rel.replace(os.sep, ".")


def _enclosing_functions(lines: list[str]) -> list[tuple[str, int, int]]:
    """返回 [(函数名, lineno, end_lineno)]（含嵌套；用 end_lineno 判包含）。"""
    tree = ast.parse("".join(lines))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node.lineno, node.end_lineno))
    return out


# 已知的 schemes.project_id 写路径（模块名 → 函数名列表）
_EXPECTED_GUARDED = {
    "app.routers.schemes": ["create_scheme", "duplicate_scheme"],
    "app.routers.bid_analysis": ["update_single_result"],
}

# 动态 SET 语句：文本里查不到 project_id，须单独锁定字段来源模型
_DYNAMIC_SET_ANCHORS = ("UPDATE schemes SET {sets}",)


# ==================================================== ① 守卫函数语义
class TestRequireProjectIdSemantics:
    """require_project_id：唯一入口的语义契约。"""

    @pytest.mark.parametrize("bad", ["", "   ", "\t\n", None])
    def test_empty_rejected_with_422(self, bad):
        from fastapi import HTTPException
        from app.routers.schemes import require_project_id
        with pytest.raises(HTTPException) as ei:
            require_project_id(bad)
        assert ei.value.status_code == 422, (
            "空 project_id 必须报 422（而不是误导的 404「项目不存在」）")
        assert "project_id" in str(ei.value.detail)

    @pytest.mark.parametrize("raw,expect", [
        ("  p1  ", "p1"),
        ("\tp2\n", "p2"),
        ("p3", "p3"),
    ])
    def test_valid_normalized_to_stripped(self, raw, expect):
        from app.routers.schemes import require_project_id
        assert require_project_id(raw) == expect

    def test_single_source_defined_once(self):
        """守卫只能有一个定义（否则「唯一入口」名存实亡）。"""
        hits = []
        for p in _iter_app_py():
            if "def require_project_id(" in open(p, encoding="utf-8").read():
                hits.append(os.path.relpath(p, APP).replace(os.sep, "/"))
        assert hits == ["app/routers/schemes.py"], (
            f"require_project_id 在多处定义（应只有 schemes.py 一份）: {hits}")


# ============================================ ② ③ 写路径守卫接线 + 全仓扫描
def _writes_scheme_project_id(window: str) -> bool:
    """判断一条写 schemes 的语句是否**写 project_id 列**。

    只认 **SET 子句列名 / INSERT 列名**，避免把 ``WHERE project_id=?`` 的
    参数、或相邻行的绑定参数误判成「写 project_id」—— 假阳性会逼后人把
    护栏改松（§5.14：护栏判据要指向真正要拦的形态）。
    """
    import re
    m = re.search(r"INSERT\s+INTO\s+schemes\s*\(([^)]*)\)", window,
                  re.I)
    if m:
        cols = [c.strip().split()[0] for c in m.group(1).split(",") if c.strip()]
        return "project_id" in cols
    m = re.search(r"UPDATE\s+schemes\s+SET\s+(.+?)(?:\s+WHERE\b)", window,
                  re.I | re.S)
    if m:
        cols = [c.strip().split("=")[0].strip() for c in m.group(1).split(",")]
        return "project_id" in [c for c in cols if c]
    return False


def _statements_writing_scheme_project_id(path: str) -> list[dict]:
    """找出文件中所有触及 schemes 表的写语句（含是否**写** project_id 列）。"""
    lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
    out = []
    for i, line in enumerate(lines):
        if "schemes" not in line or ("INSERT" not in line
                                     and "UPDATE" not in line):
            continue
        # 取 6 行窗口：SQL 常跨行拼接，且参数在语句之后的下一行
        window = "".join(lines[i:i + 6])
        out.append({"lineno": i + 1, "text": line.strip(),
                    "writes_project_id": _writes_scheme_project_id(window)})
    return out


class TestSchemeProjectIdWritePaths:
    """写 schemes.project_id 的路径必须过 require_project_id。"""

    def test_known_write_paths_call_guard(self):
        for mod, fns in _EXPECTED_GUARDED.items():
            for fn in fns:
                src = _func_src(mod, fn)
                assert "require_project_id(" in src, (
                    f"{mod}.{fn} 写 schemes.project_id 但未调用守卫 —— G3 回归")

    def test_bid_analysis_guard_precedes_try_block(self):
        """bid_analysis 的 except 会把 HTTPException 吞成 500，守卫必须在 try 外。

        ⚠️ 本函数内还有更早的 ``try:``（JSON 解析），故按「包住 UPDATE 的
        那一次 try」定位，而不是取全函数第一个 try。
        """
        src = _func_src("app.routers.bid_analysis", "update_single_result")
        anchor = "real_pid = require_project_id(real_pid)"
        assert anchor in src, "守卫调用未接入 update_single_result"
        lines = src.splitlines()
        g = next(i for i, l in enumerate(lines) if anchor in l)
        u = next(i for i, l in enumerate(lines)
                 if "UPDATE schemes SET project_id" in l)
        t = max(i for i in range(u) if lines[i].strip() == "try:")
        assert g < t, (
            "守卫调用在包住 UPDATE 的 try 内部 —— 422 会被 except Exception "
            "吞成 500（HTTPException 是 Exception 子类）")

    def test_whole_app_scan_no_unguarded_write(self):
        """全仓扫描：任何写 schemes.project_id 的 SQL，所在函数必须调守卫。

        这条锁能发现**未来新增**的写路径，而不只是锁死当前已知的三个函数名。
        """
        unguarded: list[str] = []
        scanned = 0
        for path in _iter_app_py():
            for stmt in _statements_writing_scheme_project_id(path):
                if not stmt["writes_project_id"]:
                    continue
                scanned += 1
                lines = open(path, encoding="utf-8").read().splitlines(
                    keepends=True)
                fns = _enclosing_functions(lines)
                enclosing = [name for name, lo, hi
                             in fns if lo <= stmt["lineno"] <= hi]
                mod = path_to_module(path)
                guarded = any(
                    "require_project_id(" in _func_src(mod, name)
                    for name in enclosing)
                if not guarded:
                    unguarded.append(
                        f"{os.path.relpath(path, APP).replace(os.sep, '/')}"
                        f":{stmt['lineno']} ({enclosing}) :: "
                        f"{stmt['text'][:80]}")
        assert scanned >= 3, (
            f"扫描到的写 project_id 语句只有 {scanned} 条 —— 锚点失效")
        assert not unguarded, (
            "存在写 schemes.project_id 但未经过 require_project_id 的路径：\n"
            + "\n".join(unguarded))

    def test_dynamic_set_source_model_has_no_project_id(self):
        """动态 SET 路径的字段来源模型不得含 project_id。"""
        from app.models import SchemeUpdate
        assert "project_id" not in SchemeUpdate.model_fields, (
            "SchemeUpdate 含 project_id —— 动态 SET 会成为未受守卫的写路径")
        # 且动态 SET 语句本身确实存在（防锚点漂移导致本用例空转）
        assert "UPDATE schemes SET {sets}" in _module_src("app.routers.schemes")

    def test_scan_anchors_are_real(self):
        """防护栏空转：断言扫描器确实能识别出那 3 条写 project_id 的语句。"""
        import importlib
        # ① 锚点字符串在真实源码中存在
        assert "INSERT INTO schemes" in _module_src("app.routers.schemes")
        assert "UPDATE schemes SET project_id=?" in \
            _module_src("app.routers.bid_analysis")
        # ② 扫描器真的能找出这 3 条（否则「无未守卫写路径」会变成空断言）
        import glob as _glob
        files = _glob.glob(os.path.join(APP, "app") + "/**/*.py",
                           recursive=True)
        # 用 list 而非 dict —— schemes.py 内有两条 INSERT，dict 会塌成一个键
        found = []
        for f in files:
            for stmt in _statements_writing_scheme_project_id(f):
                if stmt["writes_project_id"]:
                    found.append((os.path.relpath(f, APP).replace(os.sep, "/"),
                                  stmt["lineno"]))
        assert len(found) == 3, f"扫描出 {len(found)} 条，期望 3 条: {found}"
        # ③ 扫描器不会把「WHERE project_id=? 参数」误判成写列（假阳性回归锁）
        assert not _writes_scheme_project_id(
            "UPDATE schemes SET outline_source='x', status='y' WHERE id=?,"
            " (v, scheme_id, project_id))")
        assert not _writes_scheme_project_id(
            "UPDATE schemes SET word_count=? WHERE id=?")
        # ④ 三条已知路径都真的调了守卫（用真实导入对象，不靠 dir() 反射）
        for mod, fns in _EXPECTED_GUARDED.items():
            m = importlib.import_module(mod)
            for fn in fns:
                assert hasattr(m, fn), f"{mod} 缺失 {fn}"


# ============================================ ④ 运行时接线（防「守卫只写在注释」）
class TestGuardIsActuallyWired:
    """运行时验证：守卫真的拦得住空 project_id。

    静态锁只能证明「调用了 require_project_id」，这里直接调路由函数，
    证明空 project_id **确实进不去库**（G3 的根因路径）。
    """

    async def test_create_scheme_rejects_empty_project_id(self, db_conn):
        from fastapi import HTTPException
        from app.models import SchemeCreate
        from app.routers.schemes import create_scheme
        with pytest.raises(HTTPException) as ei:
            await create_scheme("", SchemeCreate(name="测试方案"), db=db_conn)
        assert ei.value.status_code == 422
        cur = await db_conn.execute("SELECT COUNT(*) FROM schemes")
        assert (await cur.fetchone())[0] == 0, (
            "空 project_id 已落库 —— G3 根因回流")

    async def test_duplicate_scheme_rejects_empty_project_id(self, db_conn):
        from fastapi import HTTPException
        from app.routers.schemes import duplicate_scheme
        with pytest.raises(HTTPException) as ei:
            await duplicate_scheme("", "s-does-not-exist", db=db_conn)
        assert ei.value.status_code == 422

    async def test_valid_project_id_is_normalized(self, db_conn):
        """正常值经守卫归一：首尾空白被剥掉后再做项目存在性校验与落库。"""
        from app.models import SchemeCreate
        from app.routers.schemes import create_scheme
        await db_conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "项目"))
        await db_conn.commit()

        # 传入带空白的项目号 → 守卫 strip 后命中项目并落库为归一形态
        row = await create_scheme(
            "  p1  ", SchemeCreate(name="测试方案"), db=db_conn)
        cur = await db_conn.execute(
            "SELECT project_id FROM schemes WHERE id=?", (row["id"],))
        assert (await cur.fetchone())[0] == "p1"

    async def test_empty_project_id_never_written(self, db_conn):
        """直接构造三条路径的上游值，确认没有一条能把空串写进库。"""
        import app.routers.schemes as sr
        # 项目存在但传入空 project_id → 422（不是 404「项目不存在」）
        await db_conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "项目"))
        await db_conn.commit()
        from fastapi import HTTPException
        from app.models import SchemeCreate
        with pytest.raises(HTTPException) as ei:
            await sr.create_scheme("", SchemeCreate(name="s"), db=db_conn)
        assert ei.value.status_code == 422
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM schemes "
            "WHERE project_id IS NULL OR TRIM(COALESCE(project_id,''))=''")
        assert (await cur.fetchone())[0] == 0

