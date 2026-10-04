"""process_cleanup 工具回归测试（2026-09-23 僵尸进程/关闭清理治理）

覆盖三件事的"根因防线"，均验证「不抛异常 + 语义正确 + 反例回归」：
  1. run_child：子进程超时后回收、绝不等满整段 sleep；
  2. kill_process_tree：显式杀进程树后进程消失；
  3. sweep_temp：只删匹配模式、不误删其它、异常不阻塞；
  4. register_shutdown_cleanup：回调执行、幂等登记、异常回调不冒泡中断关闭。
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app.utils import process_cleanup as pc


# --------------------------------------------------------------------------- #
# run_child：超时回收
# --------------------------------------------------------------------------- #
class TestRunChild:
    def test_completes_and_captures_output(self):
        r = pc.run_child([sys.executable, "-c", "print('hi')"], timeout=30)
        assert r.returncode == 0
        assert "hi" in (r.stdout or "")

    def test_timeout_kills_child_fast(self):
        """sleep(30) 用 1s 超时应被回收并抛 TimeoutExpired，而不是真等 30s。"""
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            pc.run_child(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        elapsed = time.monotonic() - t0
        # 允许收尾余量，但绝不能接近 30s（证明超时生效且子进程被回收）
        assert elapsed < 10, f"超时未生效，耗时 {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# kill_process_tree：显式回收
# --------------------------------------------------------------------------- #
class TestKillProcessTree:
    def test_kills_a_live_child(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pid = proc.pid
        assert pc.kill_process_tree(pid, grace_sec=5) is True
        assert pc._pid_alive(pid) is False

    def test_dead_pid_returns_true_without_error(self):
        # 复用一个必然不存在的 PID（kill 已退出的进程应安静判定为"已消失"）
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=15)
        assert pc.kill_process_tree(proc.pid, grace_sec=2) is True

    def test_invalid_pid_is_noop(self):
        assert pc.kill_process_tree(0, grace_sec=1) is True


# --------------------------------------------------------------------------- #
# assign_pid_to_kill_job：不抛异常（结果随平台/权限而变，不做强断言）
# --------------------------------------------------------------------------- #
class TestAssignJob:
    def test_never_raises(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ok = pc.assign_pid_to_kill_job(proc.pid)
            assert isinstance(ok, bool)
            # 非法入参也必须是 False，不抛
            assert pc.assign_pid_to_kill_job(0) is False
            assert pc.assign_pid_to_kill_job(-1) is False
        finally:
            pc.kill_process_tree(proc.pid, grace_sec=3)


# --------------------------------------------------------------------------- #
# sweep_temp：精确匹配 + 异常韧性
# --------------------------------------------------------------------------- #
class TestSweepTemp:
    def test_removes_only_matched(self, tmp_path: Path):
        (tmp_path / "a.lock").write_text("x", encoding="utf-8")
        (tmp_path / "b.pid").write_text("x", encoding="utf-8")
        (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.lock").write_text("x", encoding="utf-8")

        n = pc.sweep_temp([tmp_path], patterns=("*.lock", "*.pid"))
        assert n == 2
        assert not (tmp_path / "a.lock").exists()
        assert not (tmp_path / "b.pid").exists()
        assert (tmp_path / "keep.txt").exists()
        # 非递归（dirs=False）：子目录里的 lock 不动
        assert (tmp_path / "sub" / "c.lock").exists()

    def test_dirs_mode_removes_matching_dirs(self, tmp_path: Path):
        d = tmp_path / ".pt_scratch"
        d.mkdir()
        (d / "inner.txt").write_text("x", encoding="utf-8")
        n = pc.sweep_temp([tmp_path], patterns=(".pt_*",), dirs=True)
        assert n == 1
        assert not d.exists()

    def test_nonexistent_root_is_safe(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist"
        assert pc.sweep_temp([missing], patterns=("*.lock",)) == 0

    def test_max_age_skips_fresh(self, tmp_path: Path):
        f = tmp_path / "fresh.lock"
        f.write_text("x", encoding="utf-8")
        # 要求至少 3600s 之前才删 —— 刚建的文件必须保留
        n = pc.sweep_temp([tmp_path], patterns=("*.lock",), max_age_sec=3600)
        assert n == 0
        assert f.exists()


# --------------------------------------------------------------------------- #
# register_shutdown_cleanup：执行 / 幂等 / 异常不冒泡
# --------------------------------------------------------------------------- #
class TestShutdownCleanup:
    def test_callback_runs_and_is_idempotent(self):
        calls = {"n": 0}

        def cb():
            calls["n"] += 1

        before = len(pc._shutdown_callbacks)
        pc.register_shutdown_cleanup(cb)
        pc.register_shutdown_cleanup(cb)  # 同一函数重复登记应被去重
        assert len(pc._shutdown_callbacks) == before + 1
        assert cb in pc._shutdown_callbacks

        pc._run_shutdown_cleanup()
        assert calls["n"] == 1

        # 清理，避免污染其它用例（本模块的回调列表是进程级全局）
        pc._shutdown_callbacks.remove(cb)

    def test_failing_callback_does_not_raise(self):
        def boom():
            raise RuntimeError("boom")

        pc.register_shutdown_cleanup(boom)
        try:
            # 关闭流程绝不因单个回调异常而中断
            pc._run_shutdown_cleanup()
        finally:
            pc._shutdown_callbacks.remove(boom)

    def test_register_without_callback_installs_atexit(self):
        # 不传回调也应安装 atexit 兜底钩子且返回 None，不抛
        assert pc.register_shutdown_cleanup() is None
        assert pc._hooks_installed is True
