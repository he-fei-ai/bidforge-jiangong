"""审核与预检模块的数据库访问**单一出口**（R13 判空收口，2026-10-06）。

## 为什么需要这一层

AGENTS.md §5.5 记录的 R13 事故：全局单写连接 + aiosqlite 下 ``db.execute()``
**可能返回 None**（连接 / 事务瞬时异常），直接 ``await cur.fetchone()`` 即
``AttributeError``。此前本仓已逐模块收口 18 处（``doc_pipeline`` /
``routers.prompts`` / ``_chart_pipeline`` / ``routers.projects`` /
``routers.charts`` / ``pipeline.ingest_parse_result`` …），**审核与预检模块是
最后一个空白点** —— ``routers/compliance.py`` 33 处、``routers/review.py``
22 处、``routers/review_autofix.py`` 6 处 ``db.execute`` 调用**零判空**。

命中后的两种结局都不可接受：

* **读路径**：``cur.fetchone()`` 抛 ``AttributeError`` → 500，用户既拿不到
  预检结论也看不出原因；
* **写路径（更隐蔽、更危险）**：``None`` 不是异常，代码继续往下走并**汇报假成功**。
  本模块的 7 处写路径逐一核对过：

  ==================================  ======================================
  站点                              假成功后的用户可见后果
  ==================================  ======================================
  ``review.py`` 批量/单章 UPDATE      返回 ``changed=True`` / ``changed_ids``，
                                      界面提示「已标记为通过」，库里状态未变
  ``review.py`` ``_write_record``    留痕 INSERT 未生效，轨迹永久缺一条
  ``review.py`` submit UPDATE         返回 ``ok=True``，方案审核状态未推进
  ``review.py`` ``reset_review_...``  函数返回 ``True``，正文已改却仍挂「已审核」
  ``review_autofix`` ``_persist_fixed`` 返回快照 id，界面报「已修复」，正文未落库
  ``compliance.py`` ``_persist_run`` INSERT 未生效但 commit 成功 → 总检趋势
                                      静默丢一条（G2 要保护的东西）
  ==================================  ======================================

## 契约

三个出口，语义互不重叠：

* :func:`fetch_one` / :func:`fetch_all` / :func:`fetch_scalar` —— **读路径**。
  游标为 ``None`` 即抛 **503**「数据服务暂时不可用」（不是空结果）。理由：
  DB 真的不可用时返回「本方案没有章节 / 没有问题」这类空结果，会让用户以为
  方案本身干净 —— 那是**比 500 更坏**的假绿。
* :func:`exec_write` —— **写路径**。游标为 ``None`` 抛 503；游标有效但
  ``rowcount == 0`` 时按 ``require_rows`` 决定：默认 ``True`` 直接抛 503，
  因为本模块的每处 UPDATE 都已在调用前校验过目标行存在，「0 行」只可能是
  「本次写没生效」，此时汇报成功 = 静默丢数据。

``fastapi.HTTPException`` 是 ``Exception`` 的子类，因此各fail-soft 内部的
``except Exception``（``_content_fingerprint`` / ``_facts_signature`` /
``_readiness_overview_compute`` 的分段聚合）仍会把 503 降级为
「跳过该段 + WARNING」，这正是这些内部函数既有的降级语义 —— 端点直接调用
（``_build_preflight_context`` / ``_load_sections`` …）则把 503 如实抛给前端。

护栏：``tests/test_review_r13_closeout_20261006.py`` 以 AST 静态扫描禁止
三个 router 重新出现裸 ``db.execute``，并对每条写路径做行为实证
（代理连接返回 ``None`` → 必须 503，**不得**返回成功体）。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from fastapi import HTTPException

from app.db import safe_rowcount

logger = logging.getLogger("review_db")

#: DB 不可用时的统一对外文案（前端按状态码 503 提示重试，不按 detail 匹配）。
DB_UNAVAILABLE_DETAIL = "数据服务暂时不可用，请稍后重试"


def _unavailable(what: str, scheme_hint: str = "") -> HTTPException:
    logger.warning("db.execute 返回 None（R13），%s 本次不可用%s",
                   what, f"（scheme={scheme_hint[:8]}）" if scheme_hint else "")
    return HTTPException(503, DB_UNAVAILABLE_DETAIL)


async def fetch_all(db, sql: str, params: Sequence[Any] = (), *,
                    what: str = "查询") -> list[dict]:
    """读路径：取多行，统一返回 ``list[dict]``（空结果 = ``[]``）。

    ``what`` 只用于告警文案（说明是哪类查询失败），便于日志定位。
    """
    cur = await db.execute(sql, params)
    if cur is None:
        raise _unavailable(what)
    return [dict(r) for r in await cur.fetchall()]


async def fetch_one(db, sql: str, params: Sequence[Any] = (), *,
                    what: str = "查询") -> dict | None:
    """读路径：取单行，无匹配时返回 ``None``（**不是**「查询失败」）。"""
    cur = await db.execute(sql, params)
    if cur is None:
        raise _unavailable(what)
    row = await cur.fetchone()
    return dict(row) if row is not None else None


async def fetch_scalar(db, sql: str, params: Sequence[Any] = (),
                       default: Any = None, *, what: str = "查询") -> Any:
    """读路径：取首行首列（计数 / 标量聚合）。无结果返回 ``default``。"""
    row = await fetch_one(db, sql, params, what=what)
    if not row:
        return default
    return next(iter(row.values()), default)


async def exec_write(db, sql: str, params: Sequence[Any] = (), *,
                     what: str = "写操作",
                     require_rows: bool = True) -> int:
    """写路径：执行 INSERT / UPDATE / DELETE，返回受影响行数。

    Args:
        require_rows: ``True``（默认）表示调用方**已校验目标行存在**，
            ``rowcount == 0`` 即判定「本次写没生效」并抛 503 —— 绝不汇报
            假成功。仅当「0 行是合法结果」时才传 ``False``（如按条件批量
            清理、DELETE 无匹配），此时由调用方自行判读返回值。
    """
    cur = await db.execute(sql, params)
    n = safe_rowcount(cur, what=what)
    if n <= 0 and require_rows:
        logger.warning("%s 影响 0 行（判定为未生效，不汇报成功）", what)
        raise HTTPException(503, DB_UNAVAILABLE_DETAIL)
    return n


def rows_to_dicts(rows: Iterable[Any]) -> list[dict]:
    """``sqlite3.Row`` 序列 → ``list[dict]``（与 :func:`fetch_all` 同口径）。"""
    return [dict(r) for r in rows]


def row_values(row: dict) -> list:
    """取一行的**值**列表（按 SELECT 列序）。

    ⚠️ 本函数存在的理由（2026-10-06 实测踩到）：``sqlite3.Row`` 可迭代出
    **值**，而 :func:`fetch_all` 返回的 ``dict`` 迭代出的是**键**。
    ``_content_fingerprint`` 原本写的是 ``"|".join(str(v) for v in row)``，
    换成 dict 后就变成对**列名**做哈希 —— 正文一个字没改，指纹却恒定不变，
    于是「结论是否已过期」全链路静默失效（tests 的 stale 断言当场拦下）。
    凡是要按列序取值的地方，必须显式走本函数，不要直接迭代行对象。
    """
    return list(row.values())


__all__ = [
    "DB_UNAVAILABLE_DETAIL",
    "exec_write",
    "fetch_all",
    "fetch_one",
    "fetch_scalar",
    "row_values",
    "rows_to_dicts",
]