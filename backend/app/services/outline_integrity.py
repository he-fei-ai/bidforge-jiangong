"""目录父子关系完整性扫描（R184 · 方案 B：启动期只扫描、不修复）。

背景：``sections.parent_id`` 在 schema 层没有任何约束（``TEXT DEFAULT ''``，
无 CHECK、无 FK，见 schema_sql.py），父子不变量完全靠应用代码守。历史脏数据
（update_section 环检测上线前的写入、外部脚本直改库、旧快照回退/方案复制）会
留下三类坏链：

  * ``SELF_PARENT`` —— ``parent_id == id``（自父，一元环）；
  * ``ORPHAN``      —— ``parent_id`` 非空但父行不存在；
  * ``CYCLE_NODE``  —— 位于长度 ≥ 2 的父链环上（自父不计入此类，避免重复计数）。

本模块**只报告，绝不修复**（方案 B）。结果同时写入日志与进程内登记，
``GET /api/v1/system/outline-integrity`` 可查询。状态严格三态：

  * ``not_scanned`` —— 本进程尚未扫描（或刚被重置）；
  * ``completed``   —— 扫描完成，``counts`` 为真实计数（可以全是 0）；
  * ``failed``      —— 扫描未完成，``counts`` 恒为 ``null``。

**绝不以「0 条」掩盖扫描失败**：任何异常都落到 ``failed`` 并带 ``error``。
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime

logger = logging.getLogger("outline_integrity")

SCAN_KINDS: tuple[str, ...] = ("SELF_PARENT", "ORPHAN", "CYCLE_NODE")
# 每类最多回传的样本条数（够定位即可，避免响应体被脏数据撑爆）
_SAMPLE_LIMIT = 20

_NOT_SCANNED: dict = {
    "status": "not_scanned",
    "scanned_at": None,
    "duration_ms": None,
    "total_sections": None,
    "schemes": None,
    "counts": None,
    "samples": None,
    "error": None,
    "started_at": None,
}

_last_scan: dict = copy.deepcopy(_NOT_SCANNED)


def reset_scan_state() -> None:
    """把登记状态置回 ``not_scanned``（进程启动与测试使用）。"""
    global _last_scan
    _last_scan = copy.deepcopy(_NOT_SCANNED)


def get_last_scan() -> dict:
    """返回最近一次扫描结果（深拷贝，调用方改动不影响登记）。"""
    return copy.deepcopy(_last_scan)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _finish_failed(started_at: str, t0: float, error: str) -> dict:
    """登记失败状态：counts 保持 None，绝不折叠成 0 条。"""
    global _last_scan
    _last_scan = {
        "status": "failed",
        "scanned_at": None,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        "total_sections": None,
        "schemes": None,
        "counts": None,
        "samples": None,
        "error": error,
        "started_at": started_at,
    }
    logger.error("目录父子关系完整性扫描未完成（计数不可用，非 0 条）: %s", error)
    return get_last_scan()


def classify_section_links(rows: list[dict]) -> dict:
    """纯函数：对 ``(id, scheme_id, parent_id)`` 行做三类分类。

    三类互斥（同一行只计入一类）：
      · 自父（``parent_id == id``）只计入 ``SELF_PARENT``，不计入 ``CYCLE_NODE``；
      · 父行不存在只计入 ``ORPHAN``（其父链到此为止，不可能成环）；
      · 其余节点沿父链走，落在长度 ≥ 2 的环上的每个成员计入 ``CYCLE_NODE``。
    """
    by_id = {r["id"]: r for r in rows}
    ids = set(by_id)
    parent = {r["id"]: (r.get("parent_id") or "") for r in rows}

    self_parent = [by_id[i] for i in ids if parent[i] == i]
    orphan = [by_id[i] for i in ids if parent[i] and parent[i] not in ids]

    cycle: list[dict] = []
    done: set[str] = set()
    for start in ids:
        if start in done:
            continue
        path: list[str] = []
        index: dict[str, int] = {}
        cur = start
        while cur and cur not in done:
            if cur in index:
                # 命中环：path 从首次出现处到最后都是环成员
                cycle.extend(by_id[n] for n in path[index[cur]:])
                break
            pid = parent.get(cur, "")
            if not pid or pid not in ids or pid == cur:
                break
            index[cur] = len(path)
            path.append(cur)
            cur = pid
        done.update(path)

    counts = {
        "SELF_PARENT": len(self_parent),
        "ORPHAN": len(orphan),
        "CYCLE_NODE": len(cycle),
    }

    def _sample(items: list[dict]) -> list[dict]:
        return [
            {"id": r["id"], "scheme_id": r.get("scheme_id"), "parent_id": r.get("parent_id") or ""}
            for r in items[:_SAMPLE_LIMIT]
        ]

    return {
        "counts": counts,
        "samples": {
            "SELF_PARENT": _sample(self_parent),
            "ORPHAN": _sample(orphan),
            "CYCLE_NODE": _sample(cycle),
        },
    }


async def scan_section_links(db) -> dict:
    """扫描全表 ``sections`` 的父子链（只读、不修复），登记并返回结果。

    ``db`` 为任意 aiosqlite 连接（启动期由 ``run_startup_scan`` 取只读连接）。
    任何读取异常都登记 ``failed``（``counts=None``），不抛出、不折成 0。
    """
    started_at = _now()
    t0 = time.monotonic()
    try:
        cur = await db.execute("SELECT id, scheme_id, parent_id FROM sections")
        fetched = await cur.fetchall()
        # 不依赖连接的 row_factory（启动期/测试/裸连接都可能不同）：
        # 有 keys 的按列名取，否则按 SELECT 顺序取（id, scheme_id, parent_id）。
        rows = [
            {"id": r["id"], "scheme_id": r["scheme_id"], "parent_id": r["parent_id"]}
            if hasattr(r, "keys")
            else {"id": r[0], "scheme_id": r[1], "parent_id": r[2]}
            for r in fetched
        ]
        await cur.close()
    except Exception as e:  # noqa: BLE001 — 扫描失败必须显性登记
        return _finish_failed(started_at, t0, f"{type(e).__name__}: {e}")

    global _last_scan
    try:
        classified = classify_section_links(rows)
    except Exception as e:  # noqa: BLE001 — 分类器自身异常同样不得折成 0
        return _finish_failed(started_at, t0, f"{type(e).__name__}: {e}")

    counts = classified["counts"]
    _last_scan = {
        "status": "completed",
        "scanned_at": _now(),
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        "total_sections": len(rows),
        "schemes": len({r.get("scheme_id") for r in rows}),
        "counts": counts,
        "samples": classified["samples"],
        "error": None,
        "started_at": started_at,
    }

    dirty = sum(counts.values())
    if dirty:
        logger.warning(
            "目录父子关系扫描完成（只报告不修复）：sections=%d schemes=%d "
            "SELF_PARENT=%d ORPHAN=%d CYCLE_NODE=%d（%.1fms）样本=%s",
            len(rows),
            _last_scan["schemes"],
            counts["SELF_PARENT"],
            counts["ORPHAN"],
            counts["CYCLE_NODE"],
            _last_scan["duration_ms"],
            {k: [s["id"] for s in v[:3]] for k, v in classified["samples"].items() if v},
        )
    else:
        logger.info(
            "目录父子关系扫描完成：sections=%d schemes=%d，三类脏链均为 0（%.1fms）",
            len(rows),
            _last_scan["schemes"],
            _last_scan["duration_ms"],
        )
    return get_last_scan()


async def run_startup_scan() -> dict:
    """启动期入口：取只读连接跑一次扫描；取连接失败也显性登记 ``failed``。"""
    from app.db import get_read_conn, release_read_conn

    reset_scan_state()
    try:
        conn = await get_read_conn()
    except Exception as e:  # noqa: BLE001
        return _finish_failed(_now(), time.monotonic(), f"{type(e).__name__}: {e}")
    try:
        return await scan_section_links(conn)
    finally:
        try:
            await release_read_conn(conn)
        except Exception:  # noqa: BLE001 — 归还连接失败不影响扫描结果
            logger.warning("目录完整性扫描：归还只读连接失败", exc_info=True)
