"""OMP 任务存储与输入指纹缓存（引入 AGENT 引擎方案 · 落盘骨架）。

对应《专项方案编制软件引入 AGENT 引擎方案》§7.3「持久任务与断点」+ §2.3 G-2「输入指纹缓存」：

* ``input_fingerprint`` —— 对多段输入取稳定 sha256（字段间以 ``\\x1f`` 分隔），作为缓存键；
* ``OmpTaskStore``      —— 按 ``task_key`` 持久化工作流状态（JSON 文件），
  支持断点续跑与产物缓存；
* ``cache_key``         —— 由「方案 id + 阶段 + 输入摘要」派生缓存键。

⚠️ 纯文件存储（JSON），零迁移、零 DDL、零新增配置项；损坏文件静默降级
   （对齐 ``ompi_memory.MemoryBank`` 口径）。
⚠️ 分层约束：services 层禁止 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

logger = logging.getLogger("ai_provider.omp_task_store")

#: 字段分隔符（避免 ``"ab"+"c"`` 与 ``"a"+"bc"`` 同串）。
_FP_SEP = "\x1f"


def input_fingerprint(*parts: object) -> str:
    """对多段输入取稳定 sha256（前 16 位）。

    字段间以 ``\\x1f`` 分隔，避免拼接歧义；逐字节稳定，可用于缓存键。
    """
    joined = _FP_SEP.join(str(p or "") for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def cache_key(*, scheme_id: str, stage: str, inputs: object = "") -> str:
    """由「方案 id + 阶段 + 输入摘要」派生缓存键。"""
    return input_fingerprint(scheme_id, stage, inputs)


class OmpTaskStore:
    """按 ``task_key`` 的 JSON 文件任务存储（线程安全，损坏静默降级）。

    用法（断点续跑 / 产物缓存）：

        store = OmpTaskStore("backend/data/omp_tasks.json")
        key = cache_key(scheme_id=sid, stage="outline", inputs=brief)
        if store.is_resumable(key):
            state = store.load(key)          # 续跑
        artifact = store.cached_artifact(key)  # 命中即复用，不重复烧 AI
    """

    def __init__(self, path: str | None = None):
        self._path = path
        self._lock = threading.RLock()
        self._states: dict[str, dict] = {}
        self._load()

    @property
    def path(self) -> str:
        return self._path or ""

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._states = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            # 文件损坏时静默降级为空存储（宁可丢缓存不崩编排）。
            self._states = {}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._states, f, ensure_ascii=False, indent=2)
        except OSError:
            # 写失败不抛：缓存是增强，不影响主路径。
            return

    # ---------- 读写 ----------
    def save(self, task_key: str, state: dict) -> bool:
        """写入任务状态（记录 updated_at）。"""
        key = str(task_key or "")
        if not key:
            return False
        with self._lock:
            self._states[key] = dict(state or {})
            self._states[key]["updated_at"] = time.time()
            self._save()
            return True

    def load(self, task_key: str) -> dict | None:
        """读取任务状态；不存在返回 None。"""
        with self._lock:
            st = self._states.get(str(task_key or ""))
            return dict(st) if isinstance(st, dict) else None

    def clear(self, task_key: str) -> bool:
        """清除任务状态（幂等）。"""
        key = str(task_key or "")
        with self._lock:
            if key in self._states:
                del self._states[key]
                self._save()
                return True
            return False

    def is_resumable(self, task_key: str) -> bool:
        """是否可续跑（状态存在且未标记 failed/abandoned/completed）。"""
        st = self.load(task_key)
        if not st:
            return False
        return str(st.get("status") or "") not in ("failed", "abandoned", "completed")

    def cached_artifact(self, task_key: str) -> dict | None:
        """取已完成任务的产物缓存（仅 status=completed 且产物非空）。"""
        st = self.load(task_key)
        if not st or str(st.get("status") or "") != "completed":
            return None
        artifact = st.get("artifact")
        return dict(artifact) if isinstance(artifact, dict) and artifact else None
