# -*- coding: utf-8 -*-
"""R57 · 静态卫生棘轮（2026-10-09）

本机 Node 通道打通后顺带跑了一次 CI 的**后端**门禁命令（`ruff check .`，工作目录
backend），结果推翻了「lint 门禁全绿」的假设：**HEAD 工作树就有 76 条**，其中
8 条是缺陷形态（F401 死导入 6 / F811 重复定义 1 / F601 重复字典键 1），其余 68 条
是 I001 import 排序。本轮修掉 8 条缺陷形态（剩 68 条 I001 全为机械风格项，属独立
的整仓排序提交，不在本轮范围）。

本文件干三件事：
  A 把 CI 的真实判据（复用 ruff.toml，**不另加 --select**）变成护栏：缺陷类规则
    必须为 0，I001 存量不得超过记录的基线数（棘轮，只准降不准升）
  B/C 把两处**观察到的缺陷形态**抽成与 ruff 无关的 AST 扫描（duplicate 顶层定义 /
    duplicate 字典键），任何机器都能红，不依赖 ruff 是否安装
  D 锁 resolve_chunk_size 只剩一份且真的有返回值 —— 死副本的特征是「只有 docstring
    的函数」，纯文本比对抓不到「两份都长一样」的下一次复发

⚠️ 护栏自身的坑（本轮实测）：CLI 传 `--select` 会**绕过 ruff.toml 的 ignore**
（实测 --select E4 把已 ignore 的 E402 报出 50 条）。所以 A 组必须复用仓库配置，
否则会把「刻意放宽的兼容写法」误报成缺陷 —— 与 R55「判据必须能判别」同族。
"""
import ast
import io
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BE = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_BE, "app")
_TESTS = os.path.join(_BE, "tests")

# 缺陷类规则（抓到就是真 bug，不是风格）
DEFECT_CODES = ("F401", "F601", "F811", "F821")
# 2026-10-09 实测存量；只准下降，任何新增都会红
I001_BASELINE = 68

_RUFF = shutil.which("ruff") or (
    "ok" if __import__("importlib").util.find_spec("ruff") else None)


def _py_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "data", "logs")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _read_text(path):
    """utf-8-sig：容忍历史文件的 BOM（12 个，均在 HEAD 里就存在）。

    CPython 的导入机制本身会吃 BOM，所以 BOM 不破坏运行；若判据用 utf-8 读，
    护栏会被 BOM 炸掉（SyntaxError）—— 那是判据的缺陷，不是被测代码的缺陷。
    """
    return io.open(path, encoding="utf-8-sig").read()


def _tree(path):
    return ast.parse(_read_text(path), filename=path)


# ===========================================================================
# A0 · 字节形态普查（app 硬零 / 历史存量棘轮）
# ===========================================================================
_FE_SRC = os.path.abspath(os.path.join(_BE, os.pardir, "frontend", "src"))
BOM_BASELINE = {"tests": 4, "frontend_src": 8}


def _files_with_exts(root, exts):
    """独立遍历（不复用 _py_files：它只收 .py，用它普查 .ts/.tsx 会得到空集 → 恒绿）"""
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
        for name in files:
            if name.endswith(exts):
                out.append(os.path.join(dirpath, name))
    return out


def _bom_files(root, exts=(".py", ".ts", ".tsx")):
    return [os.path.relpath(p, _BE) for p in _files_with_exts(root, exts)
            if io.open(p, "rb").read(3) == b"\xef\xbb\xbf"]


class TestByteShapeCensus:

    def test_production_app_has_no_bom(self):
        """生产代码（app/）必须零 BOM —— 那里 162 个文件本来就是干净的。"""
        assert _bom_files(_APP, (".py",)) == []
        # 判别力自证：普查器必须真能看见 BOM（否则上面那条是空集恒绿）
        assert len(_bom_files(_TESTS)) == BOM_BASELINE["tests"]

    def test_legacy_bom_debt_does_not_grow(self):
        """历史存量按棘轮：只准降不准升（12 个 BOM 经 git show HEAD 逐字节核对，
        全部早于本轮存在；为「统一形态」去重写它们属无关 churn）。"""
        n_tests = len(_bom_files(_TESTS))
        n_fe = len(_bom_files(_FE_SRC))
        assert n_tests <= BOM_BASELINE["tests"], n_tests
        assert n_fe <= BOM_BASELINE["frontend_src"], n_fe

    def test_scanner_tolerates_bom(self, tmp_path):
        """判别力自证：带 BOM 的源码必须能被 _tree 解析（护栏自身不得被 BOM 炸）。"""
        p = os.path.join(str(tmp_path), "bom_probe.py")
        with io.open(p, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + "def f():\n    return 1\n".encode("utf-8"))
        assert _tree(p) is not None
        # 反向：若读取出口退回 utf-8，同一文件会当场 SyntaxError
        try:
            ast.parse(io.open(p, encoding="utf-8").read(), filename=p)
        except SyntaxError:
            pass
        else:
            raise AssertionError("utf-8 读取竟然没被 BOM 炸 —— 本用例已失去意义")


# ===========================================================================
# A · CI 判据棘轮（复用 backend/ruff.toml，不加 --select）
# ===========================================================================
def _ruff_codes(blob: str):
    """从 concise 输出里取规则代码 —— 只认「.py:行:列: 代码」形态。

    刻意**不**按路径前缀过滤：ruff 以 cwd 为基准打印**相对**路径，用绝对路径
    startswith 会解析出空集合，让「零缺陷」断言在空集上恒绿。
    """
    codes = []
    for ln in blob.splitlines():
        st = ln.strip()
        i = st.find(".py:")
        if i < 0:
            continue
        rest = st[i + 4:]
        parts = rest.split(": ", 1)
        if len(parts) < 2:
            continue
        tail = parts[1].split(" ", 1)[0]
        if tail and tail[0].isalpha() and tail[1:].isdigit():
            codes.append(tail)
    return codes


@pytest.mark.skipif(not _RUFF, reason="环境未安装 ruff（CI 会 pip install ruff==0.16.2）")
class TestRuffRatchet:

    @staticmethod
    def _run():
        # --no-cache：J 盘 .ruff_cache 写入偶发「拒绝访问」（AGENTS R54 记录），
        # 缓存只影响速度不影响判定，禁用可让护栏在任何机器上口径一致。
        return subprocess.run([sys.executable, "-m", "ruff", "check", ".",
                               "--no-cache", "--output-format=concise"],
                              cwd=_BE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)

    def test_defect_rules_are_zero(self):
        r = self._run()
        blob = (r.stdout or "") + (r.stderr or "")
        # 🔧 R58 修复：全绿时 ruff 输出 "All checks passed!"，codes 为空。
        #    旧逻辑 `assert codes` 会把"清零成功"误判为"判据失效"。
        #    正确语义：ruff 返回码 0 = 全过直接通过；非 0 时才解析 codes 查缺陷。
        if r.returncode == 0:
            return  # 全绿，缺陷类规则自然为零
        codes = _ruff_codes(blob)
        assert codes, "ruff 非零退出但解析不出规则代码：%s" % blob[-1200:]
        bad = sorted(c for c in codes if c in DEFECT_CODES)
        assert bad == [], "缺陷类规则回归：%s\n%s" % (bad, blob[-2000:])

    def test_i001_debt_does_not_grow(self):
        r = self._run()
        # 🔧 R58 修复：I001 已清零，下界从 0 放宽到允许 0（清零是目标，不是故障）。
        #    棘轮语义保留：n 不得超过基线；超过即红。
        codes = _ruff_codes((r.stdout or "") + (r.stderr or ""))
        n = len([c for c in codes if c == "I001"])
        assert n <= I001_BASELINE, (
            "I001 存量 %d 超过基线 %d（棘轮只准降不准升）"
            % (n, I001_BASELINE))

    def test_parser_reads_relative_path_lines(self):
        """判别力自证：按 ruff 真实 concise 形态（相对路径 + 反斜杠分隔）喂一行。"""
        sep = chr(92)
        sample = ("app" + sep + "main.py:46:5: I001 [*] Import block is un-sorted "
                  "or un-formatted\n"
                  "tests" + sep + "t.py:1:1: F401 [*] `x` imported but unused\n"
                  "Found 2 errors.\n")
        assert _ruff_codes(sample) == ["I001", "F401"]


# ===========================================================================
# B · 同名顶层定义（F811 形态的通用判据，不依赖 ruff）
# ===========================================================================
class TestNoDuplicateTopLevelDefs:

    @staticmethod
    def _dups(path):
        tree = _tree(path)
        seen, out = {}, []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen:
                    out.append("%s: %s@%d 与 @%d 重名" %
                               (os.path.basename(path), node.name, node.lineno, seen[node.name]))
                seen[node.name] = node.lineno
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in seen:
                        pass          # 模块级重新赋值太常见，不在本判据范围
        return out

    def test_app_has_no_duplicate_top_level_defs(self):
        hits = []
        for p in _py_files(_APP):
            hits += self._dups(p)
        assert hits == [], hits

    def test_detector_can_see_the_original_defect(self):
        """判别力自证：把 R57 修掉的形态注回一段源码，扫描器必须报出来。"""
        src = "def f():\n    return 1\n\n\ndef f():\n    \"\"\"只有 docstring\"\"\"\n"
        tree = ast.parse(src)
        seen, dups = {}, []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if node.name in seen:
                    dups.append(node.name)
                seen[node.name] = node.lineno
        assert dups == ["f"]


# ===========================================================================
# C · 重复字典键字面量（F601 形态的通用判据）
# ===========================================================================
class TestNoDuplicateDictKeyLiterals:

    @staticmethod
    def _dup_keys_in(tree):
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            seen = set()
            for k in node.keys:
                if k is None:
                    continue                      # {**other} 展开
                try:
                    val = ast.literal_eval(k)
                    hashable = True
                except Exception:
                    val = ast.dump(k)
                    hashable = False
                key = ("v", val) if hashable else ("ast", val)
                if key in seen:
                    out.append((node.lineno, key))
                seen.add(key)
        return out

    def test_backend_has_no_repeated_key_literals(self):
        hits = []
        for p in list(_py_files(_APP)) + list(_py_files(_TESTS)):
            for lineno, key in self._dup_keys_in(_tree(p)):
                hits.append("%s:%d %r" % (os.path.basename(p), lineno, key))
        assert hits == [], hits

    def test_detector_can_see_the_original_defect(self):
        tree = ast.parse('x = {("s9", "labor"): 1, ("s9", "labor"): 1}\n')
        assert len(self._dup_keys_in(tree)) == 1
        # 展开与不同键不得误报
        assert self._dup_keys_in(ast.parse('x = {**a, "b": 1}\n')) == []
        assert self._dup_keys_in(ast.parse('x = {"a": 1, "b": 1}\n')) == []


# ===========================================================================
# D · resolve_chunk_size 只剩一份，且真的有返回值
# ===========================================================================
class TestChunkSizeResolverIsSingleAndLive:

    def test_single_definition(self):
        p = os.path.join(_APP, "services", "facts_extractor.py")
        tree = _tree(p)
        defs = [n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "resolve_chunk_size"]
        assert len(defs) == 1, [d.lineno for d in defs]

    def test_body_is_not_docstring_only(self):
        """死副本的**特征**就是「函数体只有 docstring」→ 调用返回 None。
        光锁「定义只有一份」锁不住「留下的那份是空壳」这一半。"""
        p = os.path.join(_APP, "services", "facts_extractor.py")
        tree = _tree(p)
        fn = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "resolve_chunk_size"][0]
        assert len(fn.body) > 1 or not isinstance(fn.body[0], ast.Expr)
        assert any(isinstance(n, ast.Return) for n in ast.walk(fn)), "无 return → 恒返 None"

    def test_gate_off_returns_input_unchanged(self):
        """行为锁：门控关闭时原样返回传入值（默认 8000），绝不返回 None ——
        这条是「死副本若成为唯一副本」的直接判别。"""
        sys.path.insert(0, _BE)
        try:
            from app.services import facts_extractor as fe
            assert fe.resolve_chunk_size() == fe.CHUNK_SIZE
            assert fe.resolve_chunk_size(1234) == 1234
        finally:
            sys.path.remove(_BE)
