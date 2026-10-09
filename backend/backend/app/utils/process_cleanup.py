"""进程与临时资源清理工具（纯标准库，Windows 优先）

背景（2026-09-23 事故 + 本次僵尸 pytest 排查）：
  1. 在 Windows 上 `subprocess.run/Popen` 派生的子进程（ruff、mmdc、soffice、
     uvicorn --reload 的 worker……）默认**不会**随父进程异常退出而结束——父进程被
     强杀 / 崩溃 / 会话结束时，子进程成为孤儿继续持有文件句柄（典型：占用
     logs/backend.log 使日志轮转冻结 7 小时）与网络句柄（典型：孤儿 worker 继承
     监听 socket，端口看着被一个"已死"的父 PID 占住）。
  2. 只做 `timeout=` 只能覆盖"父进程还活着、正常等到超时"这一路径；父进程一旦被
     杀，`communicate(timeout=)` 根本来不及执行，子进程照漏。

本模块提供两类根因防线 + 一组关闭清理能力：
  - Windows Job Object（`KILL_ON_JOB_CLOSE`）：把子进程放进一个"随本进程句柄关闭
    即回收全部成员"的作业对象。本进程无论是正常退出、崩溃、还是被 TerminateProcess
    强杀，内核都会关闭作业句柄 → 作业内所有子进程被系统终止。**这是"父死子终"的
    唯一可靠 OS 级手段**。
  - `run_child(cmd, timeout)`：带超时 + terminate→kill 进程树 + 自动入作业对象的
    子进程执行器，替代裸 `subprocess.run(..., timeout=)`。
  - `kill_process_tree(pid)`：Windows 用 `taskkill /T /F`，POSIX 用 `killpg`。
  - `register_shutdown_cleanup(...)` / `sweep_temp(...)`：关闭时清理临时文件、缓存、
    锁文件；全部 best-effort，异常只记日志，绝不阻塞或抛出中断关闭流程。

兼容性：新增模块，不改动任何既有调用点；默认行为向后兼容（仅在显式调用时生效）。
"""
from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

logger = logging.getLogger("process_cleanup")

IS_WIN = sys.platform == "win32"

# --------------------------------------------------------------------------- #
# Windows Job Object：KILL_ON_JOB_CLOSE（父进程句柄关闭即回收全部子进程）
# --------------------------------------------------------------------------- #
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_CLOSE_HANDLE_ACCESS = 0x10000000  # PROCESS_ALL_ACCESS 足够 Assign 使用


if IS_WIN:

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IO_COUNTER(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTER),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.windll.kernel32
    _kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.SetInformationJobObject.restype = ctypes.c_bool
    _kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
    _kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _kernel32.CloseHandle.restype = ctypes.c_bool
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

# 进程级唯一作业句柄：只要本进程存活句柄就不关；本进程结束时由内核回收，
# 连带终止所有已加入作业的子孙进程。用列表持有避免被 GC 提前关句柄。
_kill_job_handle: Optional[int] = None


def _ensure_kill_job() -> Optional[int]:
    """惰性创建（并复用）带 KILL_ON_JOB_CLOSE 的作业对象，返回其句柄。

    创建失败时返回 None（例如极老版本 Windows / 已在作业中的场景），
    调用方据此降级为"仅 timeout 回收"，不影响主流程。
    """
    global _kill_job_handle
    if not IS_WIN:
        return None
    if _kill_job_handle is not None:
        return _kill_job_handle
    try:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.debug("CreateJobObjectW 返回空句柄，作业对象不可用")
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(handle)
            logger.debug("SetInformationJobObject 失败，作业对象不可用")
            return None
        _kill_job_handle = handle
        return handle
    except Exception as e:  # noqa: BLE001 - 清理机制本身绝不能拖垮调用方
        logger.debug("初始化回收作业对象异常（降级为仅超时回收）: %s", e)
        return None


def assign_pid_to_kill_job(pid: int) -> bool:
    """把指定 PID 的子进程加入"父死子终"作业对象。成功返回 True。

    注意：Windows 若该进程**已经**属于另一个作业（例如被 uvicorn/服务控制器纳入），
    则 `AssignProcessToJobObject` 会失败（Windows 8 前不允许嵌套作业），本函数安静
    返回 False，交由 timeout 兜底。
    """
    if not IS_WIN or not pid or pid <= 0:
        return False
    job = _ensure_kill_job()
    if not job:
        return False
    proc_handle = None
    try:
        proc_handle = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc_handle:
            return False
        return bool(_kernel32.AssignProcessToJobObject(job, proc_handle))
    except Exception as e:  # noqa: BLE001
        logger.debug("加入回收作业失败 pid=%s: %s", pid, e)
        return False
    finally:
        if proc_handle:
            try:
                _kernel32.CloseHandle(proc_handle)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 进程树回收
# --------------------------------------------------------------------------- #
def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0


def kill_process_tree(pid: int, grace_sec: float = 3.0) -> bool:
    """终止进程及其全部子孙进程。返回是否确认已消失。

    Windows：`taskkill /T /F /PID`（递归杀进程树，覆盖 mmdc→puppeteer→Chrome 之类
    孙进程）。POSIX：优先 `killpg`（需子进程为组长，见 run_child 的 start_new_session），
    失败降级为单进程 SIGTERM→SIGKILL。
    """
    if not pid or pid <= 0:
        return True
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=8,
                creationflags=_no_window_flags(),
            )
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception as e:  # noqa: BLE001 - 回收失败不应抛出中断业务
        logger.debug("kill_process_tree(pid=%s) 异常: %s", pid, e)

    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    if IS_WIN:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=_no_window_flags(),
            ).stdout or ""
            return str(pid) in out
        except Exception:  # noqa: BLE001
            return True  # 探测失败时保守视为存活
    try:
        os.kill(pid, 0)  # 信号 0 仅探活
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 带超时 + 父死子终的子进程执行器
# --------------------------------------------------------------------------- #
def run_child(
    cmd: Sequence[str],
    timeout: float,
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    text: bool = True,
    encoding: Optional[str] = "utf-8",
    errors: str = "replace",
) -> subprocess.CompletedProcess:
    """执行子进程：入"父死子终"作业 + 强制超时 + 超时后回收整棵进程树。

    与裸 `subprocess.run(..., timeout=)` 的差异：
      1. 子进程一被创建就加入本进程的 Job Object（KILL_ON_JOB_CLOSE），即使本进程
         （pytest / uvicorn）随后被强杀，OS 也会带走这个子进程及其后代；
      2. 超时后先 terminate、再 kill_process_tree 递归清理，绝不留孤儿；
      3. 超时抛 `subprocess.TimeoutExpired`（语义与 subprocess.run 一致）。
    """
    popen_kwargs: dict = dict(
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
    )
    if text:
        popen_kwargs.update(encoding=encoding, errors=errors)
    if IS_WIN:
        popen_kwargs["creationflags"] = _no_window_flags()
    else:
        # 让子进程自成进程组，killpg 才能连坐回收其派生的孙进程
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(list(cmd), **popen_kwargs)  # noqa: S603
    # 创建后立刻入作业对象（覆盖父进程被强杀的路径）
    assign_pid_to_kill_job(proc.pid)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        try:
            # 再收一次，回收可能已经写出的残余输出，同时避免僵尸
            stdout, stderr = proc.communicate(timeout=3)
        except Exception:  # noqa: BLE001
            stdout, stderr = (None, None) if not text else ("", "")
        raise subprocess.TimeoutExpired(list(cmd), timeout, output=stdout, stderr=stderr)

    return subprocess.CompletedProcess(list(cmd), proc.returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# 关闭清理：临时文件 / 缓存 / 锁文件 + 退出钩子
# --------------------------------------------------------------------------- #
def sweep_temp(
    roots: Iterable[os.PathLike | str],
    patterns: Iterable[str] = ("*.tmp", "*.lock", "*.pid", "~$*"),
    *,
    max_age_sec: float = 0.0,
    dirs: bool = False,
) -> int:
    """按通配模式删除临时/缓存/锁文件（best-effort，绝不抛出中断关闭流程）。

    Args:
        roots: 要扫描的目录（不存在的目录安静跳过）。
        patterns: fnmatch 通配模式集合。
        max_age_sec: >0 时仅删除"最后修改早于 now-max_age"的文件，避免误删在用的；
                     0 表示不按时间过滤（调用方对目标模式自负其责）。
        dirs: True 时把匹配到的**目录**也一并删除（如 pytest 的 .pt_* 临时目录）。

    Returns:
        实际删除的条目数（失败不计）。
    """
    removed = 0
    patterns = list(patterns)
    now = time.time()
    for root in roots:
        try:
            base = Path(root)
            if not base.exists():
                continue
            iterator: Iterable[Path] = base.rglob("*") if dirs else base.glob("*")
            for entry in iterator:
                try:
                    name = entry.name
                    is_dir = entry.is_dir()
                    if is_dir and not dirs:
                        continue
                    if not any(fnmatch.fnmatch(name, pat) for pat in patterns):
                        continue
                    if max_age_sec > 0 and (now - entry.stat().st_mtime) < max_age_sec:
                        continue
                    if is_dir:
                        _rmtree_quiet(entry)
                    else:
                        entry.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue  # 竞态：已被其它流程删除
                except Exception as e:  # noqa: BLE001 - 单个删除失败不影响其余
                    logger.debug("清理跳过 %s: %s", entry, e)
        except Exception as e:  # noqa: BLE001
            logger.debug("扫描清理目录失败 root=%s: %s", root, e)
    if removed:
        logger.info("关闭清理：删除临时/锁/缓存条目 %d 个", removed)
    return removed


def _rmtree_quiet(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# 已注册的关闭清理回调（幂等：同一 fn 只登记一次）
_shutdown_callbacks: list[Callable[[], None]] = []
_hooks_installed = False
_original_handlers: dict[int, object] = {}


def _run_shutdown_cleanup() -> None:
    """执行所有已注册清理回调。全程 best-effort，异常只记日志、绝不阻塞关闭。"""
    for cb in list(_shutdown_callbacks):
        try:
            cb()
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭清理回调 %s 异常（不阻塞关闭）: %s",
                           getattr(cb, "__name__", repr(cb)), e)


def register_shutdown_cleanup(
    callback: Optional[Callable[[], None]] = None,
    *,
    install_signals: bool = False,
) -> Optional[Callable[[], None]]:
    """登记关闭时执行的清理回调（幂等）。

    - 始终通过 `atexit` 兜底（正常解释器退出、sys.exit、未捕获异常都会触发）；
    - `install_signals=True` 时额外挂 SIGINT / SIGTERM /（Windows）SIGBREAK，
      转交给 _run_shutdown_cleanup 后调用原处理器。**默认 False**：uvicorn / 服务
      控制器本就自管信号做优雅关闭，抢占信号反而可能打断 lifespan；仅在无框架
      托管的独立脚本场景显式开启。
    返回被登记的回调（用于后续注销或断言）。
    """
    global _hooks_installed
    if callback is not None and callback not in _shutdown_callbacks:
        _shutdown_callbacks.append(callback)

    if not _hooks_installed:
        import atexit
        atexit.register(_run_shutdown_cleanup)
        _hooks_installed = True

    if install_signals:
        _install_signal_handlers()

    return callback


def _install_signal_handlers() -> None:
    sigs = [signal.SIGINT, signal.SIGTERM]
    # SIGBREAK 仅 Windows 有（控制台 Ctrl+Break / 关闭事件）
    break_sig = getattr(signal, "SIGBREAK", None)
    if break_sig is not None:
        sigs.append(break_sig)
    for sig in sigs:
        try:
            if sig in _original_handlers:
                continue
            prev = signal.getsignal(sig)

            def _handler(signum, frame, _prev=prev):  # noqa: ANN001
                _run_shutdown_cleanup()
                # 交回原处理器，保持框架/终端既有的中断语义（不要吞掉 Ctrl+C）
                if callable(_prev):
                    try:
                        _prev(signum, frame)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    # 原处理器是默认行为：恢复默认并重新抛信号，让 OS 正常收尾
                    try:
                        signal.signal(signum, signal.SIG_DFL)
                        os.kill(os.getpid(), signum)
                    except Exception:  # noqa: BLE001
                        pass

            signal.signal(sig, _handler)
            _original_handlers[sig] = prev
        except (ValueError, OSError, RuntimeError) as e:
            # 非主线程无法装信号处理器（signal.signal 只能在主线程调用）——安静降级
            logger.debug("注册信号 %s 处理器失败（降级为仅 atexit）: %s", sig, e)
