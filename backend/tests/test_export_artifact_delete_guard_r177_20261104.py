"""R177 · 导出域「产物删除唯一出口」护栏（AST 扫描）。

背景：导出产物的删除必须连带删除旁车（``<产物>.audit.json``），唯一出口是
``export._remove_export_artifact``。此前这条约束只写在注释里 —— 未来任何人
新增第二条裸删路径（``Path(...).unlink()`` / ``os.remove`` / ``shutil.move``）
都会让旁车变孤儿，且没有任何测试会红。本文件把它变成**可执行护栏**：

  · 扫描导出域全部源码（``routers/export*.py`` + ``routers/_export_*.py``）；
  · 任何 ``.unlink()`` / ``os.remove`` / ``os.unlink`` / ``shutil.move`` /
    ``shutil.rmtree`` 调用，若同行没有
    ``# R177-DELETE-EXEMPT: <原因>`` 标记 ⇒ 违规；
  · 标记必须有非空原因，且必须落在被判定为删除调用的同一行 ——
    「散在别处的一句注释」不算白名单。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import app.routers.export as E
import pytest

APP_DIR = Path(E.__file__).resolve().parents[1]
MARKER = "R177-DELETE-EXEMPT"
_MARKER_RE = re.compile(re.escape(MARKER) + r":\s*(?P<reason>.*)$")


def export_domain_files() -> list[Path]:
    routers = APP_DIR / "routers"
    return sorted({*routers.glob("export*.py"), *routers.glob("_export*.py")})


def _call_kind(node: ast.Call) -> str | None:
    f = node.func
    if not isinstance(f, ast.Attribute):
        return None
    base = f.value
    if isinstance(base, ast.Name) and base.id == "os" and f.attr in ("remove", "unlink"):
        return "os." + f.attr
    if isinstance(base, ast.Name) and base.id == "shutil" and f.attr in ("move", "rmtree"):
        return "shutil." + f.attr
    if f.attr == "unlink":
        return "unlink"
    return None


def scan(src: str, filename: str = "<src>") -> tuple[list[str], list[str]]:
    """返回 (违规列表, 白名单原因列表)。"""
    tree = ast.parse(src)
    lines = src.splitlines()
    violations: list[str] = []
    reasons: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _call_kind(node)
        if kind is None:
            continue
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        m = _MARKER_RE.search(line)
        if m is None:
            violations.append(
                f"{filename}:{node.lineno} 裸删 {kind} —— 必须走 "
                f"_remove_export_artifact，或同行标注 {MARKER}: <原因>"
            )
            continue
        reason = m.group("reason").strip()
        if not reason:
            violations.append(f"{filename}:{node.lineno} {kind} 的 {MARKER} 未写原因")
        else:
            reasons.append(f"{filename}:{node.lineno} {reason}")
    return violations, reasons


# ---------------------------------------------------------------------------
# 护栏自证：造违规样例必须被抓住
# ---------------------------------------------------------------------------

_BAD = """\
from pathlib import Path
import os
import shutil


def purge(p):
    Path(p).unlink()
    os.remove(p)
    os.unlink(p)
    shutil.move(p, p + ".bak")
    shutil.rmtree(p)
"""


def test_guard_catches_all_bare_delete_forms():
    violations, _ = scan(_BAD, "bad_sample.py")
    kinds = {v.split("裸删 ", 1)[1].split(" ", 1)[0] for v in violations}
    assert kinds == {"unlink", "os.remove", "os.unlink", "shutil.move", "shutil.rmtree"}
    assert len(violations) == 5, "有裸删形态漏抓"


def test_guard_respects_inline_exemption_with_reason():
    good = (
        "from pathlib import Path\n"
        "def cleanup(p):\n"
        f"    Path(p).unlink(missing_ok=True)  # {MARKER}: 仅清理本次临时文件\n"
    )
    violations, reasons = scan(good, "good_sample.py")
    assert not violations
    assert reasons and "仅清理本次临时文件" in reasons[0]


def test_guard_rejects_marker_without_reason_or_on_other_line():
    no_reason = "from pathlib import Path\n" "def cleanup(p):\n" f"    Path(p).unlink()  # {MARKER}:\n"
    v1, _ = scan(no_reason, "no_reason.py")
    assert v1 and "未写原因" in v1[0]

    other_line = "from pathlib import Path\n" f"# {MARKER}: 想用注释逃逸\n" "def cleanup(p):\n" "    Path(p).unlink()\n"
    v2, _ = scan(other_line, "other_line.py")
    assert v2, "标记必须与被豁免调用同一行，散在注释里不算白名单"


# ---------------------------------------------------------------------------
# 现状：导出域必须零违规
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", export_domain_files(), ids=lambda p: p.name)
def test_export_domain_has_no_bare_artifact_delete(path: Path):
    violations, _ = scan(path.read_text(encoding="utf-8"), path.name)
    assert not violations, "导出域出现绕过唯一删除出口的裸删：\n" + "\n".join(violations)


def test_export_domain_scan_is_not_vacuous():
    """非空转自证：确实扫到了导出域文件，且确有被豁免的删除调用被识别。"""
    files = export_domain_files()
    assert len(files) >= 4, f"导出域文件发现数异常：{[p.name for p in files]}"
    src = (APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
    _, reasons = scan(src, "export.py")
    assert len(reasons) >= 2, "唯一删除出口的两个 unlink 未被识别为白名单"
    assert any("唯一删除出口" in r for r in reasons)
