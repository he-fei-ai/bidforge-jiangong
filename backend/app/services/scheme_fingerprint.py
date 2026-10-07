# -*- coding: utf-8 -*-
"""方案正文内容指纹（「AI 结论是否已过期」判定的**单一事实源**，2026-10-07 收口）。

历史：``routers/compliance.py::_content_fingerprint`` 内联实现，总检聚合用它判定
preflight 结论时效（G3）；而一致性扫描（``consistency_scanner``）是另一条写
``consistency_conflicts`` 的链路，若两侧各算各的指纹，跨模块比较恒失效（AGENTS.md
反复记录的「同一判据多处实现」分叉）。现把实现下沉到这里，compliance 与
consistency_scanner 都 import 本模块 —— 分叉在结构上不可能发生。

口径（判定时效语义，2026-10-06 注释明示）：
章节 (sort_order, level, title, word_count, content) + 图表 (chart_type, status)
+ 全局事实状态 (is_simulated/is_resolved/has_conflict/is_stale/updated_at)
+ 方案字数预算。任一变化都会改变指纹。

⚠️ 与 ``routers/export.py::_content_fingerprint``（缓存命中语义，含渲染器/配图
签名）**刻意不同** —— 那是注释明示的两种设计（判定时效 vs 缓存命中），跨模块
不可比属预期，勿试图强行统一。

计算失败返回空串（空指纹 = 无法判定 = 视为不过期，不误导用户，与
``_run_is_stale`` 对历史行 fail-open 的约定一致）。
"""
import hashlib
import logging

from app.services import review_db

logger = logging.getLogger("scheme_fingerprint")


async def content_fingerprint(db, scheme_id: str) -> str:
    """方案正文 / 图表 / 事实状态 / 字数预算的内容指纹。"""
    parts: list[str] = []
    try:
        parts.extend("|".join(str(v) for v in review_db.row_values(row))
                     for row in await review_db.fetch_all(
            db,
            "SELECT sort_order, level, title, word_count, content FROM sections"
            " WHERE scheme_id=? ORDER BY sort_order, level, id", (scheme_id,),
            what="内容指纹：读取章节"))
        parts.extend("|".join(str(v) for v in review_db.row_values(row))
                     for row in await review_db.fetch_all(
            db,
            "SELECT chart_type, status FROM chart_predictions WHERE scheme_id=?"
            " ORDER BY rowid", (scheme_id,), what="内容指纹：读取图表"))
        parts.extend("|".join(str(v) for v in review_db.row_values(row))
                     for row in await review_db.fetch_all(
            db,
            "SELECT is_simulated,is_resolved,has_conflict,is_stale,updated_at "
            "FROM global_facts WHERE scheme_id=? OR (project_id="
            "(SELECT project_id FROM schemes WHERE id=?) AND "
            "(scheme_id='' OR scheme_id IS NULL)) ORDER BY rowid",
            (scheme_id, scheme_id), what="内容指纹：读取全局事实状态"))
        sc = await review_db.fetch_one(
            db, "SELECT word_budget FROM schemes WHERE id=?", (scheme_id,),
            what="内容指纹：读取字数预算")
        budget = int(sc["word_budget"]) if sc and sc["word_budget"] else 0
        parts.append(f"budget={budget}")
    except Exception as e:  # noqa: BLE001 - 指纹算不出来按「无法判定」处理
        logger.warning("内容指纹计算失败（按空指纹处理）: scheme=%s err=%s", scheme_id, e)
        return ""
    return hashlib.md5("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
