"""结构化日志与全链路追踪基础设施（R-LOG）。"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_trace_id = contextvars.ContextVar("obs_trace_id", default="")
_span_id = contextvars.ContextVar("obs_span_id", default="")
_parent_span_id = contextvars.ContextVar("obs_parent_span_id", default="")
_request_id = contextvars.ContextVar("obs_request_id", default="")
_task_id = contextvars.ContextVar("obs_task_id", default="")
_project_id = contextvars.ContextVar("obs_project_id", default="")
_chapter_id = contextvars.ContextVar("obs_chapter_id", default="")
_scheme_id = contextvars.ContextVar("obs_scheme_id", default="")

def new_trace_id():
    tid = uuid.uuid4().hex[:16]; _trace_id.set(tid); return tid
def new_span(parent=""):
    sid = uuid.uuid4().hex[:12]; _parent_span_id.set(parent or _span_id.get()); _span_id.set(sid); return sid
def set_context(**kw):
    m = {"trace_id":_trace_id,"span_id":_span_id,"parent_span_id":_parent_span_id,
         "request_id":_request_id,"task_id":_task_id,"project_id":_project_id,
         "chapter_id":_chapter_id,"scheme_id":_scheme_id}
    # ✅ R142：_force=True 允许显式置空（恢复根 span 上下文时 parent="" 也必须写入，
    #    否则 set_context 的空值跳过逻辑会让退出后的 span_id 残留——真实 BUG）。
    force = kw.pop("_force", False)
    for k,v in kw.items():
        if k in m and (force or v not in (None,"")): m[k].set(str(v))
def clear_context():
    for v in (_trace_id,_span_id,_parent_span_id,_request_id,_task_id,_project_id,_chapter_id,_scheme_id): v.set("")
def context_snapshot():
    return {"trace_id":_trace_id.get(),"span_id":_span_id.get(),"parent_span_id":_parent_span_id.get(),
            "request_id":_request_id.get(),"task_id":_task_id.get(),"project_id":_project_id.get(),
            "chapter_id":_chapter_id.get(),"scheme_id":_scheme_id.get()}

REDACTION_RULES = [
    ("api_key_sk", re.compile(r"(sk-[A-Za-z0-9]{4})[A-Za-z0-9\-_]{8,}"), r"\1****"),
    ("api_key_generic", re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?([^\s'\"]{6,})['\"]?"), r"\1=****"),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_\.]{10,}"), "Bearer ****"),
    ("phone_cn", re.compile(r"(1[3-9]\d)\d{4}(\d{4})"), r"\1****\2"),
    ("id_card", re.compile(r"(\d{6})\d{8}(\d{2}[0-9Xx])"), r"\1********\2"),
    ("email_user", re.compile(r"([A-Za-z0-9._%+-]{1,3})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"), r"\1****\2"),
]

# ✅ R142（2026-10-28 · 脱敏缺口修复）：字典键名疑似敏感（pwd/password/secret/
#    token/api_key/authorization 等）时，其值**整值掩码**（而非仅匹配键值对文本）。
#    旧实现对 dict 只递归值，而「值字符串本身」不带 `key=` 前缀 ⇒
#    {"token": "eyJ..."} / {"pwd": "s3cret!"} 均**明文入日志**（真实 BUG）。
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(pwd|passwd|password|pass|secret|token|api[_-]?key|apikey|"
    r"access[_-]?token|refresh[_-]?token|authorization|bearer|"
    r"private[_-]?key|client[_-]?secret|fernet[_-]?key)")
_SECRET_MASK = "****"


def redact_sensitive(text):
    if isinstance(text, str):
        out = text
        for _n, pat, repl in REDACTION_RULES:
            try: out = pat.sub(repl, out)
            except re.error: continue
        return out
    if isinstance(text, dict):
        out = {}
        for k, v in text.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = _SECRET_MASK
            else:
                out[k] = redact_sensitive(v)
        return out
    if isinstance(text, (list, tuple)): return [redact_sensitive(x) for x in text]
    return text

def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")

class StructuredJsonFormatter(logging.Formatter):
    def format(self, record):
        try: msg = record.getMessage()
        except Exception: msg = str(record.msg)
        snap = context_snapshot()
        extra = getattr(record, "_obs_extra", None) or {}
        payload = {
            "timestamp": _now_iso(), "level": record.levelname, "module": record.name,
            "event": getattr(record, "event", None) or extra.get("event"),
            "trace_id": snap["trace_id"] or getattr(record, "trace_id", "") or extra.get("trace_id", ""),
            "span_id": snap["span_id"] or getattr(record, "span_id", "") or extra.get("span_id", ""),
            "parent_span_id": snap["parent_span_id"] or getattr(record, "parent_span_id", "") or extra.get("parent_span_id", ""),
            "request_id": snap["request_id"] or getattr(record, "request_id", "") or extra.get("request_id", ""),
            "task_id": snap["task_id"] or getattr(record, "task_id", "") or extra.get("task_id", ""),
            "project_id": snap["project_id"] or getattr(record, "project_id", "") or extra.get("project_id", ""),
            "chapter_id": snap["chapter_id"] or getattr(record, "chapter_id", "") or extra.get("chapter_id", ""),
            "duration_ms": getattr(record, "duration_ms", None) if getattr(record, "duration_ms", None) is not None else extra.get("duration_ms"),
            "status": getattr(record, "status", None) or extra.get("status"),
            "error_code": getattr(record, "error_code", None) or extra.get("error_code"),
            "error_layer": getattr(record, "error_layer", None) or extra.get("error_layer"),
            "retryable": getattr(record, "retryable", None) if getattr(record, "retryable", None) is not None else extra.get("retryable"),
            "message": redact_sensitive(msg),
            "detail": redact_sensitive(extra.get("detail") or getattr(record, "detail", None)),
        }
        for k in list(payload.keys()):
            if k not in ("timestamp","level","module","message") and payload[k] in (None, ""):
                payload[k] = None
        return json.dumps(payload, ensure_ascii=False, default=str)


class AsyncStructuredFileHandler(logging.Handler):
    def __init__(self, base_dir, *, queue_size=10000, batch_size=100, flush_interval=1.0, formatter=None):
        super().__init__()
        self.base_dir = Path(base_dir); self.base_dir.mkdir(parents=True, exist_ok=True)
        self.queue_size = queue_size; self.batch_size = batch_size; self.flush_interval = flush_interval
        self.setFormatter(formatter or StructuredJsonFormatter())
        self._queue = None; self._dropped = 0; self._worker_task = None; self._running = False; self._loop = None
    def _ensure_queue(self):
        try:
            try: loop = asyncio.get_running_loop()
            except RuntimeError: loop = asyncio.get_event_loop()
        except RuntimeError:
            return None
        if self._queue is None or self._loop is not loop:
            self._queue = asyncio.Queue(maxsize=self.queue_size); self._loop = loop
            if self._worker_task is None or self._worker_task.done():
                try: self._worker_task = loop.create_task(self._worker()); self._running = True
                except RuntimeError: return None
        return self._queue
    async def _worker(self):
        batch = []; last = time.monotonic()
        while self._running:
            try:
                try:
                    rec = await asyncio.wait_for(self._queue.get(), timeout=self.flush_interval)
                except asyncio.TimeoutError:
                    rec = None
                if rec is not None: batch.append(rec)
                now = time.monotonic()
                if (len(batch) >= self.batch_size or (batch and now-last >= self.flush_interval)):
                    await self._flush_batch(batch); batch=[]; last=now
            except asyncio.CancelledError:
                if batch: await self._flush_batch(batch)
                break
            except Exception as e:
                try:
                    mem = self.base_dir / "_buffer.jsonl"
                    with open(mem, "a", encoding="utf-8") as f:
                        for line in batch: f.write(line+"\n")
                except Exception: pass
                batch = []
                logging.getLogger("observability").warning("结构化日志写入失败，已降级内存缓冲: %s", e)
        if batch: await self._flush_batch(batch)
    async def _flush_batch(self, batch):
        if not batch: return
        day = datetime.now().strftime("%Y-%m-%d")
        path = self.base_dir / f"{day}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                for line in batch: f.write(line+"\n")
        except Exception as e:
            logging.getLogger("observability").warning("结构化日志落盘失败: %s", e)
    def emit(self, record):
        try: line = self.format(record)
        except Exception: return
        q = self._ensure_queue()
        if q is None:
            try:
                day = datetime.now().strftime("%Y-%m-%d")
                path = self.base_dir / f"{day}.jsonl"
                with open(path, "a", encoding="utf-8") as f: f.write(line+"\n")
            except Exception: pass
            return
        try: q.put_nowait(line)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logging.getLogger("observability").warning("结构化日志队列已满，已丢弃 %d 条", self._dropped)
    def close(self):
        self._running = False
        if self._worker_task is not None:
            try: self._worker_task.cancel()
            except Exception: pass
        super().close()
    @property
    def dropped_count(self): return self._dropped

def cleanup_expired_logs(base_dir, *, retention_days):
    base_dir = Path(base_dir)
    if not base_dir.exists(): return 0
    now = datetime.now(); removed = 0
    min_days = min(retention_days.values()) if retention_days else 7
    for p in base_dir.glob("*.jsonl"):
        try: d = datetime.strptime(p.stem, "%Y-%m-%d")
        except ValueError: continue
        if (now-d).days > min_days:
            try: p.unlink(); removed += 1
            except OSError: pass
    if removed:
        logging.getLogger("observability").info("结构化日志清理：删除 %d 个过期日志文件", removed)
    return removed

class Span:
    def __init__(self, name, *, kind="internal", attributes=None):
        self.name = name; self.kind = kind; self.attributes = attributes or {}
        self.span_id = ""; self.parent = ""; self._prev_parent = ""; self.start = 0.0; self.status = "ok"; self.error = None
    def __enter__(self):
        self.parent = _span_id.get(); self._prev_parent = _parent_span_id.get()
        self.span_id = new_span(self.parent); self.start = time.monotonic()
        set_context(span_id=self.span_id, parent_span_id=self.parent); return self
    def __exit__(self, exc_type, exc, tb):
        dur = (time.monotonic()-self.start)*1000.0
        if exc is not None: self.status = "error"; self.error = str(exc)[:500]
        rec = logging.getLogger("observability.span")
        extra = {"event": f"span.{self.kind}", "span_id": self.span_id, "parent_span_id": self.parent,
                 "duration_ms": round(dur,2), "status": self.status,
                 "detail": {"name": self.name, "kind": self.kind, "attributes": redact_sensitive(self.attributes), "error": self.error}}
        rec.info(self.name, extra={"_obs_extra": extra})
        # ✅ R142：_force=True 显式恢复（根 span 退出时 parent/prev 均为 ""，空值跳过
        #    会使 span_id 残留本 span id，嵌套上下文失真）。
        set_context(span_id=self.parent, parent_span_id=self._prev_parent, _force=True)
        return False
    async def __aenter__(self): return self.__enter__()
    async def __aexit__(self, exc_type, exc, tb): return self.__exit__(exc_type, exc, tb)

def span(name, *, kind="internal", attributes=None):
    return Span(name, kind=kind, attributes=attributes)



import random


def should_sample(level: str) -> bool:
    """DEBUG 级按采样率采样；其它级别全量。"""
    try:
        from app.config import settings
        rate = getattr(settings, "structured_log_sample_debug", 1.0)
    except Exception:  # noqa: BLE001
        rate = 1.0
    if str(level).upper() != "DEBUG":
        return True
    try:
        return random.random() < float(rate)
    except (TypeError, ValueError):
        return True
