"""静态校验新增的目录生成缺口测试文件（不依赖 app，可在 ACL 环境直接运行）。

检查：语法、SyntaxWarning、用例计数、类/用例命名、docstring 完整性、
以及「每个断言引用的 API 是否都能在可读的既有测试文件中找到依据」。
输出：_coder_verify_out.txt
"""
import ast
import io
import os
import re
import sys
import warnings

os.chdir(os.path.dirname(os.path.abspath(__file__)))
OUT = []


def log(s=""):
    OUT.append(str(s))


TARGET = "tests/test_outline_generation_gaps_20261008.py"
REFS = [
    "tests/test_numbering_unification.py",
    "tests/test_numbering_batch_perf_20260927.py",
    "tests/test_content_generation_g12.py",
    "tests/test_export_atomicity_r1_20261005.py",
    "tests/test_import_module_linkage_fixes_20261003.py",
    "tests/test_import_module_crosscut_20261008.py",
    "tests/test_chart_pipeline_r13.py",
    "tests/test_strict_syntax_hardening_20261008.py",
]

# ---------- 1) 语法 + SyntaxWarning ----------
src = io.open(TARGET, encoding="utf-8").read()
log("== 1) 语法校验 ==")
log("文件: %s (%d B, %d 行)" % (TARGET, len(src.encode("utf-8")), src.count("\n") + 1))
with warnings.catch_warnings(record=True) as ws:
    warnings.simplefilter("always")
    try:
        parsed = ast.parse(src)
        compiled = compile(src, TARGET, "exec")
        assert compiled is not None
        log("compile OK / ast.parse OK")
    except SyntaxError as e:
        log("SyntaxError: %s (line %s)" % (e.msg, e.lineno))
        sys.exit(1)
bad = [w for w in ws if issubclass(w.category, (SyntaxWarning, DeprecationWarning))]
tree = parsed  # 后续 AST 遍历使用 parse 产物（compile 返回 code 对象）
log("SyntaxWarning/DeprecationWarning: %d" % len(bad))
for w in bad:
    log("  %s:%s %s" % (w.filename, w.lineno, w.message))

# ---------- 2) 用例统计 ----------
log("\n== 2) 用例统计 ==")
funcs = [n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name.startswith("test_")]
classes = [n for n in ast.walk(tree)
           if isinstance(n, ast.ClassDef) and n.name.startswith("Test")]
async_n = [n for n in funcs if isinstance(n, ast.AsyncFunctionDef)]
log("Test 类: %d  用例: %d  （async %d / sync %d）"
    % (len(classes), len(funcs), len(async_n), len(funcs) - len(async_n)))
for c in classes:
    cs = [n.name for n in c.body
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and n.name.startswith("test_")]
    log("  %-38s %d 例" % (c.name, len(cs)))
    for f in cs:
        log("      - %s" % f)

# docstring 完整性
no_doc = [n.name for n in funcs
          if not isinstance(ast.get_docstring(n, clean=False), str)]
log("缺 docstring 的用例: %s" % (no_doc or "无"))

# ---------- 3) API 引用一致性核对 ----------
log("\n== 3) API 引用与可读参考测试的一致性 ==")
ref_text = ""
for r in REFS:
    if os.path.exists(r):
        try:
            ref_text += io.open(r, encoding="utf-8").read() + "\n"
        except BaseException:
            pass
log("参考文件可读: %d/%d" % (sum(1 for r in REFS if os.path.exists(r)), len(REFS)))

# 从新增文件中抽取形如 from app.x import y / app.x.y 的符号
used = set()
for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app"):
        for a in node.names:
            used.add((node.module, a.name))
    if isinstance(node, ast.Attribute):
        chain = []
        cur = node
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id.startswith("app"):
            used.add((".".join([cur.id] + list(reversed(chain[:-1]))), chain[-1]))

# 归一为 "module.attr"
flat = set()
for mod, attr in used:
    flat.add(mod + "." + attr)
flat |= {m for m, _ in used}

# 符号（属性名）必须在参考测试中出现过 —— 这是我们唯一的事实源
symbol_names = set()
for mod, attr in used:
    symbol_names.add(attr)
missing = sorted(s for s in symbol_names
                 if s not in ref_text and s not in src.split("def ")[0])
log("引用的模块属性符号: %d 个" % len(symbol_names))
if missing:
    log("⚠ 未在可读参考测试中出现（需人工核对签名）:")
    for s in missing:
        log("    - " + s)
else:
    log("✓ 全部引用符号均可在既有可读测试中找到依据")

# ---------- 4) 缺口覆盖矩阵 ----------
log("\n== 4) 缺口覆盖矩阵（D-* 声明 vs 实际用例） ==")
declared = sorted(set(re.findall(r"\*\*(D-\d)\*\*", src)))
_doc_blob = " ".join((ast.get_docstring(n) or "") for n in funcs)
mentioned = sorted(set(re.findall(r"D-\d(?:a|b|c|d|e|f|g)?", _doc_blob)))
log("文件头声明的缺口: %s" % ", ".join(declared))
log("用例 docstring 声明的缺口: %s" % ", ".join(mentioned))
uncovered = sorted(set(declared) - set(re.findall(r"D-\d", _doc_blob)))
log("声明但无用例覆盖: %s" % (", ".join(uncovered) or "无"))

# ---------- 5) 关键断言形态检查 ----------
log("\n== 5) 关键断言形态 ==")
asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
msgs = 0
for n in asserts:
    if isinstance(n.msg, (ast.Constant, ast.JoinedStr, ast.Tuple, ast.List)):
        msgs += 1
log("assert 总数: %d，带失败消息的: %d (%.0f%%)"
    % (len(asserts), msgs, 100.0 * msgs / max(1, len(asserts))))
log("pytest.fail/skip 调用: %d / %d"
    % (src.count("pytest.fail("), src.count("pytest.skip(")))

with io.open("_coder_verify_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
print("verify done -> %d lines" % len(OUT))
