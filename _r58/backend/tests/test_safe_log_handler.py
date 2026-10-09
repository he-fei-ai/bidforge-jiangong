"""SafeRotatingFileHandler 单元测试（2026-09-23 事故回归）

背景：backend.log 触发 5MB 轮转时，若文件句柄被其它进程占用（dev server
运行期间在 backend/ 内跑 pytest 的真实场景），标准库 RotatingFileHandler 会
每次 emit 都尝试轮转、每次失败并**丢弃该条记录** → 日志永久冻结（实测 7 小时
零写入，事后无法归因）。SafeRotatingFileHandler 的契约：

1. 轮转成功路径与标准库行为完全一致（正常滚动出 .1 文件）；
2. 轮转失败（OSError/PermissionError）时降级为追加写，不抛异常、不丢记录；
3. 降级后持续追加写（后续记录继续可见），且 stderr 只告警一次。
"""
import logging
import os
import uuid

import pytest
from app.utils.safe_log_handler import SafeRotatingFileHandler


def _make_logger(tmp_path, max_bytes):
    """构造隔离的 logger + handler（propagate=False，不污染全局日志）。"""
    log_path = tmp_path / "app.log"
    handler = SafeRotatingFileHandler(
        str(log_path), maxBytes=max_bytes, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(f"safe-rot-{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger, handler, log_path


def _close(logger, handler):
    logger.removeHandler(handler)
    handler.close()


def test_rotation_success_matches_stdlib_behavior(tmp_path):
    """轮转成功时正常滚动出 .1 文件，且不进入降级模式（零行为差异）。

    注意口径：正常轮转允许按 backupCount 淘汰最老备份（标准库语义），
    这里只锁定「轮转发生、未降级、最新记录在主文件」。
    """
    logger, handler, log_path = _make_logger(tmp_path, max_bytes=200)
    try:
        for i in range(30):
            logger.info("line-%02d %s", i, "x" * 20)
    finally:
        _close(logger, handler)

    rotated = tmp_path / "app.log.1"
    assert rotated.exists(), "成功路径应产生轮转备份文件"
    assert handler.maxBytes == 200, "成功路径不得触发降级（maxBytes 不变）"
    assert handler._rollover_degraded is False
    # 最新记录在主文件，备份文件里有更早的记录
    main_text = log_path.read_text(encoding="utf-8")
    rot_text = rotated.read_text(encoding="utf-8")
    assert "line-29" in main_text
    assert "line-" in rot_text


def test_rollover_failure_degrades_to_append_no_loss_no_raise(tmp_path, capsys):
    """模拟句柄占用（rename/replace 均抛 PermissionError）：
    不得抛异常，记录继续追加进原文件，并禁用后续轮转。

    注意：Python 3.14 的 BaseRotatingHandler.rotate 用 os.rename（非 os.replace），
    且 doRollover 备份位移也用 os.rename —— 真实句柄占用场景两者都会失败，
    因此两者都要拒绝。
    """
    logger, handler, log_path = _make_logger(tmp_path, max_bytes=200)
    try:
        logger.info("before-rollover %s", "x" * 20)
        with pytest.MonkeyPatch.context() as mp:
            # Windows 真实事故：rename 因句柄占用抛 PermissionError（OSError 子类）
            def _deny(src, dst):
                raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")
            mp.setattr(os, "rename", _deny)
            mp.setattr(os, "replace", _deny)
            # 超过 max_bytes 触发轮转尝试 → 应降级而不是把记录全部丢弃
            for i in range(10):
                logger.info("degraded-%02d %s", i, "y" * 20)
        # 降级后（模拟占用解除前的长时间运行）：继续追加，记录不丢
        logger.info("after-degraded zzz")
    finally:
        _close(logger, handler)

    text = log_path.read_text(encoding="utf-8")
    # 不抛异常（能走到这里即通过）；所有记录都在主文件里，一条没丢
    assert "before-rollover" in text
    assert "degraded-00" in text
    assert "degraded-09" in text
    assert "after-degraded zzz" in text
    assert handler.maxBytes == 0, "降级后应禁用后续轮转"
    assert handler._rollover_degraded is True
    # stderr 降级告警只写一次
    err = capsys.readouterr().err
    assert err.count("降级为追加写") == 1


def test_keeps_appending_after_degradation(tmp_path):
    """反例回归：降级不是一次性兜底 —— 句柄长期占用（补丁全程生效）时，
    跨批次的记录必须持续可见（等价于事故里「冻结 7 小时」的场景）。"""
    logger, handler, log_path = _make_logger(tmp_path, max_bytes=100)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "rename", lambda s, d: (_ for _ in ()).throw(OSError("locked")))
            mp.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("locked")))
            for i in range(5):
                logger.info("first-batch-%d", i)
            for i in range(5):
                logger.info("second-batch-%d", i)
            for i in range(5):
                logger.info("third-batch-%d", i)
    finally:
        _close(logger, handler)

    text = log_path.read_text(encoding="utf-8")
    for i in range(5):
        assert f"first-batch-{i}" in text
        assert f"second-batch-{i}" in text
        assert f"third-batch-{i}" in text
