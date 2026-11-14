"""Oh My Pi (omp) 工作流跨 worker 文件租约（阶段 3 遗留项收口）。

⚠️ 背景：``_OMP_WORKFLOW_INFLIGHT`` / ``_OMP_OUTLINE_INFLIGHT`` 都是**进程内
   ``set[str]``**，多 uvicorn worker 部署时同一方案可被两个进程同时执行
   omp 长任务，产物相互覆盖。本仓无 Redis / 通用分布式锁（唯一 DB 租约
   ``facts_write_leases`` 绑定 project_id 与事实写语义，不复用）。

⚠️ 方案：文件系统级原子租约（``os.open(O_CREAT|O_EXCL)``），同一主机多
   worker 共享同一临时盘即可互斥，**零迁移、零 DDL、零新增配置项**。
   - 锁文件路径由 scheme_id 的 SHA-256 派生 → 杜绝路径穿越与文件名注入；
   - 锁体写 token / pid / 时间戳，TTL 到期后允许抢占僵尸锁（防进程崩溃后永久 409）；
   - 释放时校验 token，防止误删他人租约；
   - 目录不可写时返回 ``unavailable``（fail-open + WARNING），由进程内 set 兜底，
     不把「锁基建故障」变成全站 409（对齐本仓 fail-open 留痕取舍）。

⚠️ 边界：仅覆盖**同一主机**多 worker；跨主机部署仍需 Redis / DB 分布式锁。
   services 层，router-free；不落库、不触碰业务数据。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger("ai_provider.omp_lease")

#: 默认租约有效期（秒）。大于 ``omp_task_timeout=300`` 与前端 360s 超时的组合。
DEFAULT_TTL_SECONDS = 900
_LEASE_DIR_NAME = "omp_workflow_leases"


@dataclass(frozen=True)
class LeaseResult:
    """``acquire()`` 结果：``status`` 为 acquired / conflict / unavailable。"""

    status: str
    token: str = ""
    reason: str = ""


def _default_root() -> str:
    from app.config import settings

    configured = (getattr(settings, "omp_workspace_dir", "") or "").strip()
    return configured or tempfile.gettempdir()


class OmpWorkflowLease:
    """按 scheme_id 互斥的单主机跨 worker 文件租约。"""

    def __init__(
        self,
        scheme_id: str,
        *,
        root: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        self.scheme_id = str(scheme_id or "")
        self._ttl = max(1, int(ttl_seconds))
        self._dir = os.path.join(root or _default_root(), _LEASE_DIR_NAME)
        digest = hashlib.sha256(self.scheme_id.encode("utf-8")).hexdigest()[:40]
        self._lock_path = os.path.join(self._dir, f"{digest}.lock")
        self._owned_token = ""

    @property
    def lock_path(self) -> str:
        """锁文件绝对路径（诊断 / 测试用）。"""
        return self._lock_path

    def acquire(self) -> LeaseResult:
        """原子获取租约；冲突返回 conflict，基建不可用返回 unavailable。"""
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError as e:
            logger.warning("omp 工作流租约目录不可写，退回进程内防重入：%s", e)
            return LeaseResult("unavailable", reason=f"lease_dir_unavailable: {e}")

        token = uuid.uuid4().hex
        for attempt in range(2):
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 0 and self._reclaim_if_stale():
                    continue
                return LeaseResult("conflict", reason="另一个 omp 工作流任务持锁")
            except OSError as e:
                logger.warning("omp 工作流租约创建失败，退回进程内防重入：%s", e)
                return LeaseResult("unavailable", reason=str(e))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"token": token, "pid": os.getpid(), "ts": time.time()}, f)
            self._owned_token = token
            return LeaseResult("acquired", token=token)
        return LeaseResult("conflict", reason="租约被占用")

    def _reclaim_if_stale(self) -> bool:
        """仅当锁体可解析且确实超时才抢占；脏锁文件一律不抢占（fail-closed）。"""
        try:
            with open(self._lock_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts = float(data.get("ts") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        if time.time() - ts <= self._ttl:
            return False
        try:
            os.unlink(self._lock_path)
            return True
        except OSError:
            return False

    def release(self, token: str) -> bool:
        """仅持有者实例可释放；幂等，非法 token 返回 False。"""
        # 只有成功获取该租约的实例才允许释放，避免别的实例误删他人锁。
        if not token or token != self._owned_token:
            return False
        try:
            with open(self._lock_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, TypeError):
            return False
        if data.get("token") != token:
            return False
        try:
            os.unlink(self._lock_path)
            self._owned_token = ""
            return True
        except OSError:
            return False
