"""lint/format 覆盖一致性守卫（R182 · 2026-11-06）。

## 背景：一次「假绿」

仓库根 `.gitignore` 曾有 `backend/_*`。当 ruff 以 `backend` 为扫描根时，该规则
被重写成**无锚定**的 `_*`（2026-11-06 · ruff 0.6.9 实测；写成 `/backend/_*` 同样
失效），于是「任意层级、文件名以 `_` 开头」全部被排除，包括：

- `app/routers/_chart_pipeline.py` 等 9 个正式模块；
- `app/**/__init__.py` 等 8 个包初始化文件；
- `backend/scripts/_r64_measure_timeline.py`、`tests/_r38_probe_flaky_repro.py`。

后果：`ruff format --check .` 只扫 673 个文件便报「全部已格式化」（**假绿**），
显式 `ruff format --check app tests`（绕过 gitignore）才暴露 23 个待格式化文件。

根治方式：backend 专属规则迁入 `backend/.gitignore`（`/_*`、`/legacy_scripts/*`），
让 git 与 ruff 的解析根一致（见该文件头注释）。

## 本文件守护的不变量

**凡是 git 看得见的 `backend/**/*.py`（已跟踪 + 未跟踪且未被忽略），都必须落在
ruff 的实际扫描集合内。** 这同时覆盖两个方向：

- 已跟踪文件不能静默脱保（本洞曾让 31 个文件消失）；
- 新增文件在提交前就纳入覆盖，避免「先合并、后补 lint」。

反向地，被判为「git 忽略」的一次性产物（legacy_scripts/artifacts 等）允许不在
扫描面内——它们本来就不该进 lint。

判据全部取自工具**真实输出**（`git ls-files` / `ruff check --show-files`），
不做路径推断，并附「非空转」自证。

⚠️ 口径说明：`ruff.toml` 的 `extend-exclude`（tools/data/logs/_diagnostics/…）
属于**显式配置**的排除，本守卫从配置动态读取后扣除，只追究「被 gitignore 悄悄
吃掉」的那部分；因此增删该配置会同步反映到守卫口径，而不是让守卫误报。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tomllib

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
_REPO = _BACKEND.parent
_RUFF = shutil.which("ruff") or shutil.which("ruff.exe")

pytestmark = pytest.mark.skipif(_RUFF is None, reason="未安装 ruff，无法核对扫描面")


def _run(cmd: list[str], cwd: pathlib.Path) -> list[str]:
    """执行并返回非空输出行；失败时把 stderr 一并抛出，便于定位。"""
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, f"{cmd} 失败（rc={proc.returncode}）：{proc.stderr.strip()}"
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


@pytest.fixture(scope="module")
def excludes() -> set[str]:
    """ruff 显式排除的目录名（来自 ruff.toml，避免硬编码漂移）。"""
    with open(_BACKEND / "ruff.toml", "rb") as f:
        cfg = tomllib.load(f)
    return set(cfg.get("extend-exclude", []))


@pytest.fixture(scope="module")
def scanned() -> set[pathlib.Path]:
    """ruff 的实际扫描集合（与 `ruff format --check .` 同一发现口径）。"""
    files = [ln for ln in _run([_RUFF, "check", "--show-files", "."], _BACKEND) if ln.endswith(".py")]
    return {pathlib.Path(f).resolve() for f in files}


@pytest.fixture(scope="module")
def git_visible(excludes: set[str]) -> set[pathlib.Path]:
    """git 看得见的 `backend/**/*.py`：已跟踪 + 未跟踪且未被忽略，再扣除 ruff 显式排除目录。"""
    paths = _run(["git", "-C", str(_REPO), "ls-files", "--", "backend"], _REPO)
    paths += _run(["git", "-C", str(_REPO), "ls-files", "--others", "--exclude-standard", "--", "backend"], _REPO)
    visible: set[pathlib.Path] = set()
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        path = (_REPO / rel).resolve()
        # 任一祖先目录名命中 ruff 显式排除项（tools/data/logs/_diagnostics/__pycache__…）则剔除。
        if not any(part in excludes for part in path.relative_to(_BACKEND).parts[:-1]):
            visible.add(path)
    return visible


def test_scan_sets_are_not_vacuous(scanned: set[pathlib.Path], git_visible: set[pathlib.Path]):
    """前提自证：两侧都必须给出大样本，否则后续断言是空转。"""
    assert len(git_visible) >= 600, f"git 可见且属 ruff 管辖的 .py 只有 {len(git_visible)} 个，口径可疑"
    assert len(scanned) >= 600, f"ruff 只扫到 {len(scanned)} 个 .py，口径可疑"


def test_every_git_visible_py_is_scanned_by_ruff(scanned: set[pathlib.Path], git_visible: set[pathlib.Path]):
    """核心不变量：任何 git 可见的后端源码都不得逃出 lint/format 覆盖。

    该洞存在时（仓库根写 `backend/_*`）本用例必红：31 个 `_*.py`/`__init__.py`
    会从扫描面消失（另有 `scripts/_r64_measure_timeline.py` 一并脱保）。
    """
    missing = git_visible - scanned
    detail = "\n".join(f"  - {p.relative_to(_BACKEND).as_posix()}" for p in sorted(missing))
    assert missing == set(), f"{len(missing)} 个 git 可见的 .py 未进入 ruff 扫描面（lint/format 假绿）：\n{detail}"
