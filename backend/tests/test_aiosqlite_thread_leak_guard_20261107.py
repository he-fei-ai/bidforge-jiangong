"""护栏（2026-11-07）：pytest 会话结束后不得残留 aiosqlite 工作线程。

回归背景：用例全绿（如 test_prompt_material_guard_r175 的 2 条用例 2.01s
passed），但进程汇总后卡在 `threading._shutdown` 等待
`aiosqlite/core.py::_connection_worker_thread`（非守护线程），最终只能外部
杀进程收场。根因是测试直接 `await app.db.init_db()` 建出全局 `_conn` 后，
pytest 会话没有生产 lifespan 那样的 `close_db()` 收尾。

本用例用**子进程**复现最小场景：
  1. 子 pytest 跑一条只调用 `init_db()`、绝不手动关闭的用例；
  2. 父进程要求子进程在规定时间内正常退出。
若 conftest 的 `pytest_sessionfinish -> close_db()` 被回退，子进程会在
解释器退出阶段等待非守护的 aiosqlite 线程 → 超时 → 本用例转红。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
CHILD_TIMEOUT_SECONDS = 30

_CHILD_SOURCE = '''\
"""临时子进程用例：只建全局连接，不手动关闭（由 conftest 会话收尾负责）。"""
import asyncio

import app.db as db


def test_child_leaves_global_connection_open(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "child_thread_leak.db")
    asyncio.run(db.init_db())
    assert db._conn is not None, "前置条件：init_db 必须建立全局连接"
'''


def test_session_end_closes_aiosqlite_worker_thread(tmp_path):
    """子进程跑完用例后必须能正常退出（不等待残留非守护线程）。"""
    child_file = TESTS_DIR / f"test_zz_thread_leak_child_{uuid.uuid4().hex}.py"
    child_file.write_text(textwrap.dedent(_CHILD_SOURCE), encoding="utf-8")
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-o",
                    "addopts=",
                    str(child_file),
                ],
                cwd=str(BACKEND_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=CHILD_TIMEOUT_SECONDS,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise AssertionError(
                f"子进程 {CHILD_TIMEOUT_SECONDS}s 未退出 —— 会话结束未关闭 aiosqlite 连接，"
                "进程卡在 threading._shutdown 等待非守护工作线程。"
                f"\n子进程 stdout:\n{e.stdout}\n子进程 stderr:\n{e.stderr}"
            ) from e
    finally:
        child_file.unlink(missing_ok=True)

    assert proc.returncode == 0, (
        "子进程 pytest 未以 0 退出；用例可能全绿但会话收尾失败。"
        f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "1 passed" in proc.stdout, f"子进程未按预期执行用例：\n{proc.stdout}"
