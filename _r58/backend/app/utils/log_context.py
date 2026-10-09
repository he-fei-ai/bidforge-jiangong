"""全链路日志上下文（解析提取模块埋点）。

用 contextvars 在「上传 → 解析 → 提取 → 保存 → 分类 → 信息调用」的异步调用链上
透传 trace_id / project_id / scheme_id / doc_id / task_id，并通过 logging.Filter
把这些 ID 自动追加到每条日志，便于用 grep 按请求 / 文档 / 任务串联全链路。

背景（2026-09-23 日志审计）：原日志以 logger 名 + 消息为主，缺乏结构化关联 ID。
排查「某份文档上传后为何没被提取 / 某次解析为何失败」这类跨阶段问题时，无法把
分散在多行、多文件、多阶段的日志串成一条链路（日志分析报告显示 doc_id /
task_id / request_id 命中数均为 0）。本模块即补齐这一埋点缺口。

用法：
    from app.utils.log_context import new_trace_id, set_context, TraceContextFilter
    tid = new_trace_id()                      # 在处理入口生成一次
    set_context(project_id=pid, doc_id=did)   # 进入子阶段时补充
    # 在日志初始化处把 TraceContextFilter 挂到 handler 上即可。
"""
from __future__ import annotations

import contextvars
import logging
import uuid

_trace_id = contextvars.ContextVar("log_trace_id", default="")
_project_id = contextvars.ContextVar("log_project_id", default="")
_scheme_id = contextvars.ContextVar("log_scheme_id", default="")
_doc_id = contextvars.ContextVar("log_doc_id", default="")
_task_id = contextvars.ContextVar("log_task_id", default="")


def new_trace_id() -> str:
    """生成并绑定一个新的全链路追踪 ID（在处理入口调用一次）。"""
    tid = uuid.uuid4().hex[:12]
    _trace_id.set(tid)
    return tid


def set_context(
    *,
    trace_id: str = "",
    project_id: str = "",
    scheme_id: str = "",
    doc_id: str = "",
    task_id: str = "",
) -> None:
    """补充当前调用链上的关联 ID（进入子阶段时调用，空串表示不改动）。"""
    if trace_id:
        _trace_id.set(trace_id)
    if project_id:
        _project_id.set(project_id)
    if scheme_id:
        _scheme_id.set(scheme_id)
    if doc_id:
        _doc_id.set(doc_id)
    if task_id:
        _task_id.set(task_id)


def clear_context() -> None:
    """清空当前调用链上的全部关联 ID（测试隔离 / 请求收尾时调用）。"""
    _trace_id.set("")
    _project_id.set("")
    _scheme_id.set("")
    _doc_id.set("")
    _task_id.set("")


def context_suffix() -> str:
    """返回当前链路上的关联 ID 后缀，形如 ``trace=abc pid=xxx doc=yyy``。"""
    parts: list[str] = []
    t = _trace_id.get()
    if t:
        parts.append(f"trace={t}")
    p = _project_id.get()
    if p:
        parts.append(f"pid={p}")
    s = _scheme_id.get()
    if s:
        parts.append(f"sid={s}")
    d = _doc_id.get()
    if d:
        parts.append(f"doc={d}")
    k = _task_id.get()
    if k:
        parts.append(f"task={k}")
    return " ".join(parts)


class TraceContextFilter(logging.Filter):
    """把当前链路关联 ID 自动前缀到每条日志的消息上（幂等，已加过则跳过）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 同一 record（被多个 handler 处理 / 重复 emit）只前缀一次。
        if getattr(record, "_logctx_applied", False):
            return True
        record._logctx_applied = True
        suffix = context_suffix()
        if not suffix:
            return True
        # 先按原 args 渲染完整消息，再前缀关联 ID，避免破坏 %-format 参数占位符。
        rendered = record.getMessage()
        record.msg = f"[{suffix}] {rendered}"
        record.args = ()
        return True


def install_filter(logger: logging.Logger | None = None) -> None:
    """把 TraceContextFilter 挂到指定 logger（默认 root）的所有 handler 上（幂等）。"""
    target = logger or logging.getLogger()
    flt = TraceContextFilter()
    for handler in list(target.handlers):
        if any(isinstance(f, TraceContextFilter) for f in handler.filters):
            continue
        handler.addFilter(flt)
    # 同时挂到 logger 自身，覆盖直接在该 logger 上 emit 的场景
    if not any(isinstance(f, TraceContextFilter) for f in target.filters):
        target.addFilter(flt)
