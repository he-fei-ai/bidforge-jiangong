"""OMP 记忆银行适配层：``retain / recall / reflect / memory_edit`` 的平台侧等价。

对应 omp 上游能力（README「Memory the agent curates」）：
  - ``retain``   —— 会话中途写事实到记忆银行（project-scoped，默认为本项目作用域）；
  - ``recall``   —— 搜索记忆银行，取回原始记忆；
  - ``reflect``  —— 基于记忆银行综合回答；
  - ``memory_edit`` —— 按 id 更新 / 遗忘 / 失效记忆。

与 omp 的区别（对齐既有集成口径）：
  - omp 的记忆后端可选 ``local / Hindsight / Mnemopi``（SQLite）；本层为**确定性
    JSON 文件存储**（``backend/data/ompi_memory.json`` 或注入路径），无外部服务依赖，
    零迁移、零 DDL。若日后启用 Mnemopi，只需替换 ``_Storage`` 实现。
  - 本层只做**事实存取**，不做 LLM 语义检索——``recall`` 用子串 / 标签过滤，
    与 omp 的原始记忆检索同口径；跨会话上下文交接由此承载。

⚠️ 分层约束：services 层禁止 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

#: 记忆条目状态：active 可召回；forgotten 被遗忘（保留审计痕迹）。
_ACTIVE = "active"
_FORGOTTEN = "forgotten"


@dataclass
class MemoryEntry:
    """一条记忆：事实文本 + 作用域 + 标签 + 来源 + 状态。"""

    id: str
    text: str
    scope: str = "project"
    tags: list[str] = field(default_factory=list)
    source: str = ""
    status: str = _ACTIVE
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "scope": self.scope,
            "tags": list(self.tags),
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        return cls(
            id=str(d.get("id") or ""),
            text=str(d.get("text") or ""),
            scope=str(d.get("scope") or "project"),
            tags=[str(t) for t in (d.get("tags") or [])],
            source=str(d.get("source") or ""),
            status=str(d.get("status") or _ACTIVE),
            created_at=float(d.get("created_at") or 0.0),
            updated_at=float(d.get("updated_at") or 0.0),
        )


def _tag_tokens(text: str) -> set[str]:
    """从文本抽取低噪声标签 token（用于 recall 过滤）。"""
    return {t.lower() for t in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,20}", text or "")}


class MemoryBank:
    """确定性 JSON 文件记忆银行（线程安全，进程内单实例约定由调用方保证）。

    典型用法（三环节上下文交接）：
        bank = MemoryBank(path)
        bank.retain("L2 子节点数必须 3~5 个", tags=["outline", "constraint"])
        bank.retain("工期 450 日历天（见 global-facts.md）", tags=["fact"])
        hits = bank.recall("工期")        # -> 命中「工期」的记忆条目
        brief = bank.reflect("正文写作")   # -> 综合要点摘要，注入正文 Agent context
    """

    def __init__(self, path: str | None = None, *, default_scope: str = "project"):
        self._path = path
        self._default_scope = default_scope
        self._lock = threading.RLock()
        self._entries: dict[str, MemoryEntry] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("entries", []) if isinstance(raw, dict) else raw
            if isinstance(items, list):
                for d in items:
                    if isinstance(d, dict) and d.get("id"):
                        e = MemoryEntry.from_dict(d)
                        self._entries[e.id] = e
        except (OSError, ValueError):
            # 文件损坏时静默降级为空银行（宁可丢记忆不崩编排）。
            self._entries = {}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(
                    {"entries": [e.to_dict() for e in self._entries.values()]},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            # 写失败不抛：记忆是增强，不影响编排主路径。
            return

    # ---------- OMP 等价操作 ----------
    def retain(
        self, text: str, *, tags: list[str] | None = None, source: str = "", scope: str | None = None
    ) -> MemoryEntry:
        """写一条事实到记忆银行（对应 omp ``retain``）。"""
        now = time.time()
        with self._lock:
            e = MemoryEntry(
                id="m" + uuid.uuid4().hex[:12],
                text=str(text).strip(),
                scope=scope or self._default_scope,
                tags=[str(t).strip() for t in (tags or []) if str(t).strip()],
                source=source,
                status=_ACTIVE,
                created_at=now,
                updated_at=now,
            )
            self._entries[e.id] = e
            self._save()
            return e

    def recall(self, query: str, *, scope: str | None = None, limit: int = 20) -> list[MemoryEntry]:
        """搜索记忆银行（对应 omp ``recall``）：子串 / 标签过滤 + 最新优先。"""
        q = str(query or "").strip().lower()
        q_tokens = _tag_tokens(q)
        with self._lock:
            hits: list[MemoryEntry] = []
            for e in self._entries.values():
                if e.status != _ACTIVE:
                    continue
                if scope and e.scope != scope:
                    continue
                if not q:
                    hits.append(e)
                    continue
                text_l = e.text.lower()
                if q in text_l:
                    hits.append(e)
                    continue
                if q_tokens and q_tokens <= _tag_tokens(e.text) | set(t.lower() for t in e.tags):
                    hits.append(e)
                    continue
            hits.sort(key=lambda x: x.updated_at, reverse=True)
            return hits[: max(1, int(limit))]

    def reflect(self, prompt: str, *, limit: int = 10) -> str:
        """基于记忆银行综合回答（对应 omp ``reflect``）：返回要点摘要文本。"""
        hits = self.recall(prompt, limit=limit)
        if not hits:
            return ""
        lines = ["【记忆要点】"]
        for e in hits:
            tag = "、".join(e.tags[:4]) or e.scope
            lines.append(f"- [{tag}] {e.text}")
        return "\n".join(lines)

    def memory_edit(self, memory_id: str, *, new_text: str | None = None, forget: bool = False) -> bool:
        """按 id 更新 / 遗忘记忆（对应 omp ``memory_edit``）。"""
        with self._lock:
            e = self._entries.get(str(memory_id))
            if e is None:
                return False
            if forget:
                e.status = _FORGOTTEN
            elif new_text is not None:
                e.text = str(new_text).strip()
            e.updated_at = time.time()
            self._save()
            return True

    # ---------- 统计 ----------
    def stats(self) -> dict[str, int]:
        with self._lock:
            active = sum(1 for e in self._entries.values() if e.status == _ACTIVE)
            return {"total": len(self._entries), "active": active, "forgotten": len(self._entries) - active}
