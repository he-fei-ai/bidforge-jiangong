"""源码级加固警卫测试（2026-10-08）

背景：`content.py` 的 `content_generation_system` 模板在计算书示例里使用
**单反斜杠** LaTeX（`\\frac / \\varphi / \\times / \\text / \\lambda / \\leq`），
这些序列在 Python 普通三引号字符串里被解析为**控制字符**（`\\f`->FF、`\\v`->VT、
`\\t`->TAB）或无效转义（`\\l`/`\\p` SyntaxWarning）——发给 AI 的示例公式变成
`\\x0crac{N}{\\x0barphi A}`。本轮已修复为双反斜杠。本文件把这些行实时看守住，
防止以后有人把多行模板改回单反斜杠而无人察觉。

同时看护 `app/db.py` 的 WAL 自愈入口类型韧性（`DB_PATH` 被覆写为 str 时不得崩溃）。
"""
from __future__ import annotations

import ast
import glob
import warnings
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1] / "app"
_PROMPTS_DIR = _BASE / "services" / "ai" / "prompts"
#: 一定会被解析成控制字符的坏转义（单反斜杠开头）
_BAD_CONTROL = ("\x0c", "\x0b", "\x08", "\x07")


def _prompt_sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(glob.glob(str(_PROMPTS_DIR / "*.py"))):
        out[Path(p).name] = p  # 只登记存在，内容在用例内读取
    return out


def test_prompt_files_have_no_invalid_escape_warnings():
    """ai/prompts 下所有 .py 编译时不得再产生 SyntaxWarning（无效转义）。"""
    bad: list[str] = []
    for p in _PROMPTS_DIR.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            compile(src, str(p), "exec")
        for w in ws:
            if issubclass(w.category, SyntaxWarning):
                bad.append(f"{p.name}:{w.lineno or 0} {str(w.message)[:80]}")
    assert not bad, "发现无效转义(SyntaxWarning)：\n" + "\n".join(bad)


def test_prompt_template_strings_have_no_control_chars():
    """ai/prompts 模板字符串字面量不得再含 \f \v \b \a 控制字符。"""
    hits: list[str] = []
    for p in _PROMPTS_DIR.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for ch in _BAD_CONTROL:
                    if ch in node.value:
                        i = node.value.find(ch)
                        hits.append(
                            f"{p.name}@{node.lineno} 含 {ch!r}: "
                            f"...{node.value[max(0, i - 15):i + 15]!r}...")
    assert not hits, "模板字符串含控制字符：\n" + "\n".join(hits)


def test_content_generation_system_latex_is_double_escaped_in_source():
    """content_generation_system 的计算书示例在源码里必须是 **双反斜杠**
    （渲染后为单反斜杠 LaTeX），防止再次退化为控制字符。"""
    src = (_PROMPTS_DIR / "content.py").read_text(encoding="utf-8")
    # 源码中应为双反斜杠（"\\frac" 两字符，Python 字面量需写 "\\\\frac"）
    assert "\\\\frac{N}{\\\\varphi A}" in src
    assert "\\\\lambda = l_0 / i" in src
    assert "\\\\text{ kN/m}" in src
    assert "\\\\leq f = 205" in src
    assert "\\\\times 489" in src
    # 闭环验证：编译后的实际字符串值不再含控制字符（与守卫 2 一致，这里锁死内容示例段）
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if "frac{" in v or "\\varphi" in v or "长细比" in v:
                assert "\\frac{N}{\\varphi A}" in v, "渲染后应为单反斜杠 LaTeX"
                for ch in _BAD_CONTROL:
                    assert ch not in v, f"渲染后示例仍含控制字符 {ch!r}"


# ---------------------------------------------------------------------------
# app/db.py 类型韧性（WAL 自愈入口）
# ---------------------------------------------------------------------------

def test_db_as_path_normalizes_str_and_path():
    """_as_path 必须把 str 与 Path 统一归一为 pathlib.Path。"""
    from app.db import _as_path
    assert isinstance(_as_path("C:/tmp/x.db"), Path)
    assert isinstance(_as_path(r"C:\tmp\y.db"), Path)
    p = Path("C:/tmp/z.db")
    assert _as_path(p) is p


def test_self_heal_corrupt_wal_with_str_db_path_no_crash():
    """DB_PATH 被覆写为 str（测试/脚本常见做法）时，初始化自愈不得崩溃。

    回归锚点：2026-10-08 实测 `DB_PATH='.../x.sqlite'`（str）直接调用 init_db
    会在 ``_self_heal_corrupt_wal`` 的 ``DB_PATH.exists()`` 上抛 AttributeError。
    """
    import app.db as dbmod
    from app.db import _self_heal_corrupt_wal

    original = dbmod.DB_PATH
    try:
        dbmod.DB_PATH = str(Path("C:/tmp/nonexistent_db_dir_20261008.sqlite"))
        # 不存在的库 + str 形态：应安全返回 False（无 WAL、主库不存在）
        assert _self_heal_corrupt_wal() is False
        dbmod.DB_PATH = "C:/tmp/no-such-wal-20261008.db.sqlite"
        assert _self_heal_corrupt_wal() is False
    finally:
        dbmod.DB_PATH = original