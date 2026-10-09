"""轮转失败降级处理的 RotatingFileHandler（宁可日志超限，不可丢日志）

背景（2026-09-23 事故）：
logs/backend.log 达到 5MB 轮转阈值时，若文件句柄被其它进程占用（典型：dev
server 运行期间在 backend/ 内跑 pytest —— 每个 pytest 进程 import app.main
时都会创建自己的 RotatingFileHandler、打开同一个日志文件的句柄），Windows 下
轮转 rename 必然报 PermissionError。标准库 RotatingFileHandler 的行为是：
    shouldRollover() 恒真（文件已超限）→ 每次 emit 都尝试轮转 → 每次失败
→ 该条记录被丢弃 → 日志永久冻结（实测冻结 7 小时无一条写入），
事后完全无法做调用归因排查。

降级策略（本类唯一差异点）：
轮转失败（OSError，含 Windows 句柄占用导致的 PermissionError）时：
  1. 置 maxBytes = 0 禁用后续轮转（shouldRollover 对 maxBytes<=0 恒假）；
  2. 关闭当前流并以追加模式重开 —— 日志继续写进原文件；
  3. 首次降级时向 stderr 写一条告警（只写一次，不刷屏）。
轮转成功路径与标准库完全一致（零行为差异）。
"""
import sys
from logging.handlers import RotatingFileHandler


class SafeRotatingFileHandler(RotatingFileHandler):
    """轮转失败时降级为追加写，而不是静默丢弃所有后续日志记录。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 实例级降级标记：保证 stderr 告警只写一次（类属性会在多实例间串扰，
        # pytest 进程与 dev server 是不同实例，互不影响）
        self._rollover_degraded = False

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            # 典型场景：Windows 下文件被其它进程占用（rename 抛 PermissionError，
            # 属 OSError 子类）。super().doRollover 失败前可能已关闭/轮转了部分
            # 备份文件（从最老的 .N 往 .1 逐个 rename），这里统一收口：
            # 禁用轮转 + 追加写，保证业务日志不丢。
            self.maxBytes = 0
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响降级重开
                    pass
                self.stream = None
            try:
                self.stream = self._open()  # mode='a'，追加到原文件末尾
            except OSError:
                # 连重开都失败（极端：目录被删/磁盘故障）：保持 stream=None，
                # 下次 emit 会按 FileHandler.emit 的既有逻辑再尝试重开
                pass
            if not self._rollover_degraded:
                self._rollover_degraded = True
                try:
                    sys.stderr.write(
                        "logging: 轮转失败（日志文件句柄被其它进程占用？），"
                        "已降级为追加写模式（不再轮转），日志不会丢失\n")
                except Exception:  # noqa: BLE001 - 告警失败绝不影响日志主流程
                    pass
