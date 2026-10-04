"""全局事实变量路由（增强版）

增强点（基于可行性研究报告）：
1. 支持结构化字段：source_ref, is_simulated, confidence, is_resolved, has_conflict
2. 分段提取管线：长资料智能切分 → 并发提取 → 合并去重 → 矛盾检测
3. 缓存失效联动：事实变更后自动清空 export_cache
4. 模拟值闸门：正文生成前可强制拦截未确认模拟值
5. 统计 API：模拟值占比、矛盾统计、待审核统计
"""

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.config import FACT_UPLOADS_DIR, settings
from app.db import get_db, safe_rowcount
from app.models import FactGroupIn, FactGroupUpdate

# 提取项目模块的分类与阈值判定核心（本仓「分类单一事实源」，不重复维护阈值表）
from app.services import scheme_classification as scheme_clf
from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render

# ✅ 2026-09-25：文档分类唯一事实源（分类清单 / 自动分类规则 / 提取优先级）
from app.services.doc_categories import (
    AUTO_CLASSIFY_RULES,
    auto_classify_document,
    category_options,
)
from app.services.doc_pipeline import pipeline as doc_pipeline

# ✅ 2026-09-24：全局事实「九大章节分类体系」四维标注（纯函数、零 AI、零 DB 依赖）
from app.services.facts_classification import (
    CHAPTER_ORDER,
    CHAPTER_TITLES,
    FACT_ATTR_TITLES,
    SOURCE_KIND_TITLES,
    category_map_payload,
    chapter_field_completeness,
    classify_chapter_from_text,
    classify_fact_attr,
    dimensions_for_row,
    extract_danger_params,
    nine_chapter_summary,
)
from app.services.facts_extractor import (
    CATEGORY_TITLES,
    CATEGORY_TO_FACT_TYPE,
    MAX_SOURCE_EXCERPT,
    _clip_excerpt,
    _safe_confidence,
    append_simulated_marker,
    extract_value_from_markdown_line,
    invalidate_export_cache,
    is_safety_critical_name,
    is_simulated_marked,
    normalize_key,
    strip_simulated_marker,
)
from app.services.file_parser import (
    SUPPORTED_EXTENSIONS,
    ParseError,
    dump_parse_warnings,
    parse_file_content_ex,
    signature_valid,
)
from app.utils.log_context import new_trace_id, set_context

logger = logging.getLogger("global_facts")
router = APIRouter(prefix="/api/v1/global-facts", tags=["global_facts"])

# ✅ 单一事实源（2026-10-04）：上传配额不再在本模块各写一份字面量，
#    统一从 settings（可被 .env 覆盖）派生，并经
#    GET /api/v1/system/upload-limits 动态下发给前端。
#    保留模块级名字：既有用法与 monkeypatch.setattr(gf, "MAX_UPLOAD_BYTES", N)
#    的单测全部照常生效。
_DEFAULT_UPLOAD_BYTES = 30 * 1024 * 1024
_DEFAULT_UPLOAD_FILES = 20
_DEFAULT_UPLOAD_TOTAL_BYTES = 200 * 1024 * 1024


def _positive_int_setting(name: str, default: int) -> int:
    """读取正整数配置；配置缺失/非法/非正数时回落默认。

    非正数不能直接放行：files 数与累计体积若按 0 处理，等价于关闭配额守卫，
    比保守默认更危险，故一律回落。
    """
    try:
        n = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


#: 单文件上传上限（字节）
MAX_UPLOAD_BYTES = _positive_int_setting("upload_max_bytes", _DEFAULT_UPLOAD_BYTES)
# ✅ 完整性：解析结果落库上限。旧值 80000 会让长篇招标文件在解析阶段就被截断，
#    后续无论提取多少段都拿不到后半部分内容。SQLite TEXT 可容纳，放宽到 40 万字
#    （≈ 300~400 页），配合 MAX_CHUNKS=60 基本覆盖常规招标文件全文。
MAX_PARSED_CHARS = 400_000

# ✅ 资源配额：单次上传请求的文件数与累计体积上限。
#    单文件上限挡不住"一个请求塞进大量文件"——落盘与随后的解析
#    会线性放大磁盘/内存占用，且解析是串行阻塞在服务进程内的。
MAX_UPLOAD_FILES_PER_REQUEST = _positive_int_setting(
    "upload_max_files_per_request", _DEFAULT_UPLOAD_FILES)
MAX_UPLOAD_TOTAL_BYTES = _positive_int_setting(
    "upload_max_total_bytes", _DEFAULT_UPLOAD_TOTAL_BYTES)


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------

def _coerce_conflict_values(raw: str) -> list[dict]:
    """把冲突候选 JSON 解析为 [{value:str, source:str, confidence:float}]。

    ✅ 序列类候选值（list）统一转字符串，保证前端可直接渲染。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict] = []
    for c in parsed:
        if not isinstance(c, dict):
            continue
        v = c.get("value", "")
        if isinstance(v, (list, tuple)):
            v = "、".join(str(x) for x in v if x is not None)
        elif v is not None and not isinstance(v, str):
            v = str(v)
        raw_conf = c.get("confidence")
        # ✅ BUG 修复（两处）：
        #   1. `x or 1.0` 会把合法的 0 置信度吞成 1.0（0 为 falsy），
        #      模拟兜底值常为 0~0.3，被抬高后前端失去"低置信度"提示依据。
        #   2. `float(raw_conf)` 未防御脏值：模型若输出 "high"/"高" 会抛
        #      ValueError → 整个列表接口 500（所有事实都读不出来）。
        out.append({
            "value": v or "",
            "source": str(c.get("source", "") or ""),
            "confidence": _safe_confidence(raw_conf, 1.0),
            # ✅ 候选值自带的模拟值语义一并下发：前端可标注「候选亦为模拟值」，
            #    resolve_conflict 也据此重算被采纳行的 is_simulated。
            #    旧数据无该字段时按 False（候选来自文档摘录，视为真实证据）。
            "is_simulated": bool(c.get("is_simulated")),
        })
    return out


def _find_conflict_candidate(raw: str, value: str) -> dict | None:
    """在 conflict_keys 候选中按值匹配，返回候选元数据（含 is_simulated）。

    ✅ 供 resolve_conflict 用：裁决换值时按「候选值自身的模拟值语义」重算该行的
    is_simulated，而不是沿用原行的旧标记（编造值被裁决为文档实值后，
    原 is_simulated=1 会残留 → stats.simulated 永不归零）。
    """
    target = strip_simulated_marker(str(value or ""))
    for c in _coerce_conflict_values(raw):
        if strip_simulated_marker(c.get("value") or "") == target:
            return c
    return None


def _parse_source_ref(raw: str) -> list[dict]:
    """解析 source_ref 列（JSON 数组 / 旧版纯文件名字符串）为统一结构。

    ✅ BUG 修复：旧实现在 JSON 解析失败时直接返回 []，导致早期以纯文本写入
    来源的旧数据在界面上"来源"一栏永远为空，且 persist_extraction 的
    「手动录入保护」判定失效（进而被重新提取误删）。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [{"file": str(raw), "quote": ""}]
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    out: list[dict] = []
    for ref in parsed:
        if isinstance(ref, dict):
            out.append({"file": str(ref.get("file") or ""),
                        "quote": str(ref.get("quote") or "")})
        elif ref:
            out.append({"file": str(ref), "quote": ""})
    return out


def _fact_dimension_fields(row: dict, name: str, value: str, fact_key: str,
                           source_list: list, fact_type: str | None = None) -> dict:
    """派生/回读九大章节四维标注（读路径惰性兜底，纯函数、不写库）。

    口径（与提取管线 ``apply_fact_dimensions`` 一致）：
    - 库列有值 → 原样回传（尊重人工归类，不被重提取冲掉）；
    - 库列为空（2026-09-24 前的历史行 / 手工录入行）→ 按确定性规则派生。
    历史行因此不会在「章节视图」与「按章节精选正文」中丢失。

    ``fact_type`` 为空时使用行内 ``fact_type`` 列；``list_facts`` 会把已按
    ``CATEGORY_TO_FACT_TYPE`` 反查出的有效值传入，让历史行也能更准确地分章。
    """
    src = (source_list[0].get("file") if source_list else "") or \
          (row.get("source") or "")
    # ✅ 唯一实现收口（2026-09-29）：四个维度的「已落库值优先、缺失时惰性派生」
    #    判据**全部**在 dimensions_for_row 内部完成。旧实现本函数又自己写了一遍：
    #      · ``is_shared`` 双写 OR（外层 ``bool(stored) or dims`` 与内层
    #        ``bool(row) or dims`` 完全等价，冗余但无害，读起来却像有两套语义）；
    #      · ``source_kind`` 另调一次 ``classify_source_kind`` —— 因为传进去的
    #        dict 忘了带 source/source_ref，导致派生值只能拿到默认 bid_doc。
    #    现把 source/source_ref 一并传入，函数体只剩「行 → 派生输入」的适配，
    #    判据从此只有一份。
    dims = dimensions_for_row({
        "name": name,
        "value": value,
        "category": row.get("category") or "",
        # 传入的有效 fact_type 优先（list_facts 已按 CATEGORY_TO_FACT_TYPE 反查）
        "fact_type": fact_type if fact_type is not None else (row.get("fact_type") or ""),
        "fact_key": fact_key,
        "chapter": row.get("chapter") or "",
        "fact_attr": row.get("fact_attr") or "",
        "source_kind": row.get("source_kind") or "",
        "source": src,
        "source_ref": row.get("source_ref") or "",
        # is_shared 允许 None（列缺失）：dimensions_for_row 内部 bool(None)=False，
        # 按「存库 0 与缺失等价」处理，语义与旧实现一致。
        "is_shared": row.get("is_shared"),
    })
    chapter = dims.get("chapter") or ""
    return {
        # 九大章节归属（overview/basis/plan/technique/safety/personnel/
        # acceptance/emergency/calc_drawings；空串=未分类）
        "chapter": chapter,
        "chapter_title": CHAPTER_TITLES.get(chapter, ""),
        # 事实属性（quantitative 定量 / qualitative 定性 / relation 关系 / norm 规范）
        "fact_attr": dims.get("fact_attr") or "",
        # 数据来源（bid_doc/drawing/survey/overall_plan/manual）
        "source_kind": dims.get("source_kind") or "",
        # 跨章节共性事实（True = 多章节复用，避免重复提取）
        "is_shared": bool(dims.get("is_shared")),
        "shared_chapters": list(dims.get("shared_chapters") or ()),
    }


def _chapter_stats_for_items(items: list[dict]) -> dict:
    """九大章节聚合统计 + 字段完整性差集（供前端章节视图直接渲染）。

    Args:
        items: ``list_facts`` 已回解出的条目（含 name/value/chapter/fact_attr/...）。

    Returns:
        ``{"chapters": [...], "totals": {...}, "field_completeness": {...}}``
    """
    rows = [
        {"name": it.get("name", ""), "value": it.get("value", ""),
         "chapter": it.get("chapter") or "", "fact_attr": it.get("fact_attr") or "",
         "source_kind": it.get("source_kind") or "",
         "is_shared": it.get("is_shared", False),
         "category": it.get("category") or "", "fact_type": it.get("fact_type") or ""}
        for it in items
    ]
    summary = nine_chapter_summary(rows)
    # 事实属性 / 数据来源 两个维度的分布（四维分类的另两个维度）
    by_attr: dict[str, int] = {k: 0 for k in FACT_ATTR_TITLES}
    by_kind: dict[str, int] = {k: 0 for k in SOURCE_KIND_TITLES}
    for it in rows:
        a = it.get("fact_attr") or ""
        if a in by_attr:
            by_attr[a] += 1
        k = it.get("source_kind") or ""
        if k in by_kind:
            by_kind[k] += 1
    out = {
        "chapters": summary["chapters"],
        "totals": summary["totals"],
        "by_fact_attr": by_attr,
        "by_source_kind": by_kind,
        "shared_count": sum(1 for it in rows if it.get("is_shared")),
    }
    return out


async def _validate_fact_scope(db, scheme_id: str, project_id: str) -> tuple[str, str]:
    """校验事实读取作用域，返回 ``(有效 scheme_id, 有效 project_id)``。

    ``/chapters`` 与 ``/danger-check`` 历史实现允许两个 ID 都为空，最终退化为
    ``SELECT * FROM global_facts``，会把全库事实暴露给无作用域请求。这里统一做
    存在性校验：有 scheme 时反查其真实 project；仅传 project 时也必须真实存在。
    """
    sid = scheme_id.strip() if isinstance(scheme_id, str) else ""
    pid = project_id.strip() if isinstance(project_id, str) else ""
    if sid:
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (sid,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "方案不存在")
        real_pid = str(row[0] or "")
        if pid and pid != real_pid:
            raise HTTPException(400, "scheme_id 与 project_id 不匹配")
        return sid, real_pid
    if not pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    cur = await db.execute("SELECT id FROM projects WHERE id=?", (pid,))
    if not await cur.fetchone():
        raise HTTPException(404, "项目不存在")
    return "", pid


async def _assert_fact_in_scheme_scope(db, fact_id: str, scheme_id: str) -> dict:
    """确保按 fact_id 的写操作属于当前方案或其项目共享范围。

    scheme_id 为空时保留旧脚本/内部调用兼容；前端事实写接口全部传当前方案，
    防止路由切换竞态或旧弹窗误改其它方案数据。
    """
    cur = await db.execute(
        "SELECT id, project_id, scheme_id FROM global_facts WHERE id=? OR group_id=? LIMIT 1",
        (fact_id, fact_id))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "事实不存在")
    if not scheme_id:
        return dict(row)
    target_pid = str(row["project_id"] or "")
    target_sid = str(row["scheme_id"] or "")
    if target_sid:
        if target_sid != scheme_id:
            logger.warning("global_facts 409 归属校验：target_sid=%s 与 scheme_id=%s 不一致", target_sid, scheme_id)
            raise HTTPException(409, "事实不属于当前方案，请刷新后重试")
    else:
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
        current = await cur.fetchone()
        if not current or str(current[0] or "") != target_pid:
            logger.warning("global_facts 409 项目共享校验：scheme_id=%s 未关联 project_id=%s", scheme_id, target_pid)
            raise HTTPException(409, "项目共享事实不属于当前方案，请刷新后重试")
    return dict(row)


async def _invalidate_fact_scope_cache(db, scheme_id: str, project_id: str = "") -> None:
    """事实变更后失效导出缓存；项目共享事实需覆盖项目下全部方案。

    统一带 ``facts_touched=True`` —— 本函数是「全局事实被写入」的唯一失效出口，
    因此方案级 ``facts_updated_at`` 时间戳（章节失效标记的数据源）也在这里推进，
    而不是散落各处各自决定。
    """
    if scheme_id:
        await invalidate_export_cache(db, scheme_id, facts_touched=True)
        return
    if not project_id:
        return
    cur = await db.execute("SELECT id FROM schemes WHERE project_id=?", (project_id,))
    for item in await cur.fetchall():
        # ✅ BUG 修复（2026-10-01）：旧实现是 `if not item[0]: break` ——
        # 一旦命中第一个 id 为空的行就**中断整个循环**，其后所有方案的导出
        # 缓存与 facts_updated_at 全部漏失效。schemes.id 虽是主键，但 TEXT
        # 主键并不禁止 NULL，脏数据下就会触发。空 id 行应当**跳过**而非终止。
        sid = str(item[0] or "")
        if not sid:
            continue
        await invalidate_export_cache(db, sid, facts_touched=True)


async def _load_fact_rows(db, scheme_id: str, project_id: str,
                         *, injectable_only: bool = False) -> list[dict]:
    """按统一作用域取全局事实：方案私有 + 同项目共享事实。

    该集合与 ``build_injectable_facts_query`` 保持一致；仅传 project_id 时保持
    历史语义，返回该项目下全部事实。

    ✅ P0 修复（2026-09-27 · 危大判定用编造值/过期值算阈值）：
      ``injectable_only=True`` 时追加与正文注入、导出门控完全同口径的过滤
      （复用 ``facts_extractor._FACTS_INJECT_WHERE`` 单一事实源，剔除矛盾值、
      未确认模拟值 is_simulated=1、已被取代的过期值 is_stale=1）。
      ``POST /global-facts/danger-check`` 必须用此模式 —— 否则 AI 编造的参数
      会让「超过一定规模」的危大判定误报，而该误报会在前端红标展示，
      属于**用不确定数据下确定性结论**。
      默认 False：``GET /global-facts`` 是管理端列表，必须能看到全部行
      （含模拟值/过期值）供人工裁决，行为与旧版一致。
    """
    sid, pid = await _validate_fact_scope(db, scheme_id, project_id)
    scope_sql, params = "", []
    if sid and pid:
        scope_sql = (
            " WHERE (scheme_id=? OR (project_id=? AND "
            "(scheme_id='' OR scheme_id IS NULL)))"
        )
        params = [sid, pid]
    elif sid:
        scope_sql, params = " WHERE scheme_id=?", [sid]
    else:
        scope_sql, params = " WHERE project_id=?", [pid]
    if injectable_only:
        # ✅ BUG 修复（2026-09-27）：与 sse_handlers._load_facts_rows 同源同修 ——
        #   旧实现在 import 失败时回落到「has_conflict=0 AND is_resolved=1」，
        #   丢掉 is_simulated=0 / is_stale=0，使 danger-check 在降级路径下
        #   用 AI 编造值、过期值判定危大阈值（确定性结论误报，红标展示）。
        #   现统一走 get_facts_inject_where() 出口，兜底 fail-closed。
        try:
            from app.services.facts_extractor import get_facts_inject_where
            inject_where = get_facts_inject_where()
        except Exception:  # pragma: no cover - 兜底：fail-closed，不放宽门控
            inject_where = (
                "has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0")
        scope_sql += f" AND {inject_where}"
    sql = "SELECT * FROM global_facts" + scope_sql + \
        " ORDER BY category, group_id, updated_at, id"
    cur = await db.execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


@router.get("")
async def list_facts(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    limit: int = Query(0, ge=0, description="分页大小，0=返回全部（向后兼容）"),
    offset: int = Query(0, ge=0, description="分页偏移"),
    db=Depends(get_db),
):
    """查询全局事实（支持按 scheme_id 或 project_id 过滤）

    返回增强版数据，包含 category/source_ref/is_simulated/confidence/
    is_resolved/has_conflict 等字段。

    ✅ 增强（2026-10-03）：超大方案事实分组可能数百条，一次性全量返回既浪费
    带宽也拖慢首屏。新增 limit/offset 分页（默认 limit=0 即不限制，完全
    向后兼容）；统计信息 stats 始终基于全量分组计算，分页只裁剪返回的
    groups 列表，前端可据 pagination.total_groups 做分页控件。

    ✅ BUG 修复（P2 · 2026-10-04）：本 docstring 此前被写在**函数体内**
    （int() 归一之后），是一段无副作用的字符串表达式 —— 结果
    `list_facts.__doc__` 恒为 None：FastAPI 取不到描述、OpenAPI 文档里该端点
    没有说明、help()/自省工具同样看不到。现移到函数体首行（正确的 docstring 位置）。
    """
    # ✅ 容错（2026-10-03）：经 HTTP 调用时 limit 为解析后的 int；但单测或内部
    #    直接调用路由函数时，缺省值是 Query(0) 对象，直接 `limit > 0` 会抛
    #    TypeError。统一在此归一为 int，`Query` 对象无法 int() 时回落 0（不限）。
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    sql = "SELECT * FROM global_facts WHERE 1=1"
    params: list = []
    if scheme_id:
        # 与目录/正文/导出一致：方案页必须同时看到本方案事实和同项目共享事实，
        # 否则会出现「页面看不见，但生成链路实际会注入」的隐蔽数据断链。
        real_pid = ""
        if project_id:
            real_pid = project_id.strip()
        else:
            cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
            scheme_row = await cur.fetchone()
            if not scheme_row:
                raise HTTPException(404, "方案不存在")
            real_pid = str(scheme_row[0] or "")
        if real_pid:
            sql += (" AND (scheme_id=? OR (project_id=? AND "
                    "(scheme_id='' OR scheme_id IS NULL)))")
            params.extend([scheme_id, real_pid])
        else:
            sql += " AND scheme_id=?"
            params.append(scheme_id)
    elif project_id:
        sql += " AND project_id=?"
        params.append(project_id)
    if not scheme_id and not project_id:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    # ✅ 稳定排序：末尾追加 id 兜底，避免 updated_at 相同（批量插入时常见）
    #    导致同一分组内事实顺序每次刷新都抖动，用户编辑时难以定位。
    sql += " ORDER BY category, group_id, updated_at, id"

    cur = await db.execute(sql, params)
    rows = [dict(r) for r in await cur.fetchall()]

    # 按 group_id 聚合
    # ✅ BUG 修复：分组标题此前未落库，重新查询时误用单条事实名（如"项目经理"）
    #    作为分组标题（应为"人员角色"）。现优先取 group_title 列，
    #    回退 category 中文名，最后才用事实名兜底。
    def _resolve_group_title(row: dict) -> str:
        gtitle = (row.get("group_title") or "").strip()
        if gtitle:
            return gtitle
        cat = (row.get("category") or "").strip()
        return CATEGORY_TITLES.get(cat, "") or (row.get("title") or "")

    groups: dict[str, dict] = {}
    for r in rows:
        gid = r.get("group_id") or r["id"]
        if gid not in groups:
            groups[gid] = {
                "id": gid,
                "title": _resolve_group_title(r),
                # ✅ BUG 修复：group.content 需聚合【全部分行】的 Markdown 内容，
                # 供前端编辑弹窗使用。旧实现只取首行 → 前端编辑保存时后端按
                # 首行重建整个分组，分组内第 2..N 行被永久删除（数据丢失）。
                "_content_lines": [],
                "category": r.get("category", ""),
                "items": [],
            }
        elif not groups[gid]["title"]:
            groups[gid]["title"] = _resolve_group_title(r)

        # 解析单条事实
        # ✅ 兼容旧数据：source_ref 可能是 JSON 数组，也可能是早期写入的纯文件名
        source_list = _parse_source_ref(r.get("source_ref", "") or "")

        # 解析矛盾候选值（conflict_keys 列实际存储的是 conflict_values 的 JSON）
        # ✅ 兼容旧数据：value 可能是数组，统一转字符串，避免前端渲染数组报错
        conflict_values = _coerce_conflict_values(r.get("conflict_keys", "") or "")

        # 从 content 中提取结构化 name/value（兼容旧数据）
        name = r.get("title", "")
        content = r.get("content", "")
        value = ""
        m = re.match(r'^-?\s*\*\*(.+?)\*\*\s*[:：]\s*(.*)', (content or "").strip())
        if m:
            name = m.group(1)
            # ✅ BUG 修复（2026-09-21）：旧正则 `\s*\*\(?\s*(?:⚠️?\s*)?模拟值\s*\)?\*\s*$`
            #    只能从 `*` 开始匹配，而 `⚠️` 前缀写在 `*` 之前 → **残留半个标记**：
            #      "15.0m ⚠️*(模拟值)*"  → value = "15.0m ⚠️"（悬空 emoji 当正文展示）
            #      "12.5m  *(⚠ 模拟值)*" → value = "12.5m"（to_db_row 写法恰好干净）
            #    两种落库写法结果不一致，且脏值 "15.0m ⚠️" 被 _apply_item_updates /
            #    persist_extraction 当作真实取值参与「值是否变化」比较 → 凡走过
            #    PATCH（分组重建 / 单条编辑 / 矛盾裁决）或手工新增的模拟值事实，
            #    其值恒被判为「已变化」，2026-09-20 修的「只改分类/改名静默清矛盾」
            #    漏洞实际未堵住（详见 facts_extractor.SIMULATED_MARKER_RE 说明）。
            value = strip_simulated_marker(m.group(2))
        elif (content or "").strip():
            # ✅ BUG 修复：旧实现仅在 content 以 "-" 开头时才解析 name/value，
            #    非列表行（整段文本、或没写减号的 "**名称**: 值"）会把整块原文
            #    （含 Markdown 标记前缀）当作 value 回传 → 前端原样显示 "**x**: y"。
            _, value = extract_value_from_markdown_line(content)

        item = {
            "fact_id": r["id"],
            "name": name,
            "value": value or content,
            "source": source_list[0]["file"] if source_list else "",
            "source_ref": source_list[0]["quote"] if source_list else "",
            "is_simulated": bool(r.get("is_simulated", 0)),
            # ✅ 修复：confidence 列若被写入脏值（旧数据/手工 SQL），
            #    float() 会抛异常导致整个列表接口 500。
            "confidence": _safe_confidence(r.get("confidence"), 1.0),
            "is_resolved": bool(r.get("is_resolved", 1)),
            "has_conflict": bool(r.get("has_conflict", 0)),
            "conflict_values": conflict_values,
            "conflict_keys": r.get("conflict_keys", ""),
            "fact_key": r.get("fact_key", ""),
            "category": r.get("category", ""),
            # 方案私有事实与项目共享事实会同时进入当前方案页面；显式标记作用域，
            # 防止用户把跨方案共享事实误判为本方案私有数据。
            "scheme_id": r.get("scheme_id") or "",
            "scope": "project" if not (r.get("scheme_id") or "") else "scheme",
            "is_stale": bool(r.get("is_stale", 0)),
            # ✅ 增强：回传提取阶段产出的分类扩展字段。
            #    is_safety_critical 始终以程序规则复算（启发式为权威下限，覆盖历史/手动行），
            #    fact_type 优先取提取期落库的列、缺失时由 category 反查，供前端展示与注入决策使用。
            "is_safety_critical": is_safety_critical_name(
                name, value or content, r.get("fact_key", "")),
            "fact_type": (r.get("fact_type") or "") or CATEGORY_TO_FACT_TYPE.get(r.get("category", ""), ""),
            # ✅ 信息调用完整性（2026-09-23）：补全提取期已落库、SSE 已下发的溯源/单位
            #    扩展字段。旧 list_facts 仅回传部分字段，前端刷新后丢失页码溯源(page_ref)、
            #    证据类型(evidence_kind)、计量单位(value_unit)、语义区(zone_type)、
            #    归一化组(norm_group)、分段指纹(chunk_hash)。SELECT * 已含这些列，此处
            #    补齐以保证「刷新 == 流式」数据一致（不丢溯源信息）。
            "value_unit": r.get("value_unit") or "",
            "evidence_kind": r.get("evidence_kind") or "",
            "page_ref": r.get("page_ref") or "",
            "zone_type": r.get("zone_type") or "",
            "norm_group": r.get("norm_group") or "",
            "chunk_hash": r.get("chunk_hash") or "",
            # ✅ 2026-09-24：九大章节四维标注（正交于 22 类 category）。
            #    历史行（2026-09-24 前落库）没有这 4 列的值，读路径做惰性派生
            #    兜底（纯函数、不写库）—— 保证「章节视图/按章节精选」不会因为
            #    存量数据没标注而看不到事实。已标注的行取库值（尊重人工归类）。
            **_fact_dimension_fields(
                r, name, value or content, r.get("fact_key", ""), source_list,
                fact_type=(r.get("fact_type") or "")
                or CATEGORY_TO_FACT_TYPE.get(r.get("category", ""), "")),
        }
        groups[gid]["items"].append(item)
        # 累积原始 Markdown 内容行（供前端编辑弹窗使用，避免丢行）
        groups[gid]["_content_lines"].append(r.get("content", "") or "")

    group_list = list(groups.values())
    # 落定 group.content = 全部分行拼接（前端编辑弹窗依赖此字段完整回显）
    for g in group_list:
        lines = g.pop("_content_lines", []) or []
        g["content"] = "\n".join(l for l in lines if l.strip())

    # ✅ 增强：分组按类别稳定排序（此前按随机 group_id 排序 → 每次刷新顺序抖动）
    _cat_order = {c: i for i, c in enumerate(CATEGORY_TITLES.keys())}
    group_list.sort(key=lambda g: (_cat_order.get(g.get("category") or "", 999),
                                   g.get("title") or ""))

    # ✅ 增强（2026-10-03）：分页裁剪（仅影响返回的 groups，统计仍基于全量）
    total_groups = len(group_list)
    if limit > 0:
        page_groups = group_list[offset:offset + limit]
    else:
        page_groups = group_list

    # 计算统计信息
    all_items = [it for g in group_list for it in g["items"]]
    stats: dict = {
        "total": len(all_items),
        "simulated": sum(1 for it in all_items if it["is_simulated"]),
        "unresolved": sum(1 for it in all_items if not it["is_resolved"]),
        "conflicts": sum(1 for it in all_items if it["has_conflict"]),
        "safety_critical": sum(1 for it in all_items if it["is_safety_critical"]),
        "simulated_ratio": (sum(1 for it in all_items if it["is_simulated"])
                            / max(len(all_items), 1)),
        "project_shared": sum(1 for it in all_items if it.get("scope") == "project"),
        "stale": sum(1 for it in all_items if it.get("is_stale")),
    }

    # ✅ 增强：分类维度统计（"信息分类"的可观测性）。
    #    前端据此展示「各类别事实分布/风险分布」，用户可快速定位
    #    哪一类事实缺失（如 0 条"工期安排"）或模拟值集中在哪里。
    by_cat: dict[str, dict] = {}
    for g in group_list:
        cat = g.get("category") or "other"
        s = by_cat.setdefault(cat, {
            "category": cat,
            "title": CATEGORY_TITLES.get(cat, "") or cat,
            "groups": 0, "items": 0, "simulated": 0,
            "conflicts": 0, "unresolved": 0, "safety_critical": 0,
        })
        s["groups"] += 1
        for it in g["items"]:
            s["items"] += 1
            if it["is_simulated"]:
                s["simulated"] += 1
            if it["has_conflict"]:
                s["conflicts"] += 1
            if not it["is_resolved"]:
                s["unresolved"] += 1
            if it["is_safety_critical"]:
                s["safety_critical"] += 1
    stats["by_category"] = sorted(
        by_cat.values(),
        key=lambda x: (_cat_order.get(x["category"], 999), x["title"]))

    # ✅ 2026-09-24：九大章节维度统计（建办质〔2018〕31号 专项方案结构）。
    #    正交于上面的 22 类 category：category 是「事实类型」视角，
    #    chapter 是「方案章节」视角。前端「章节视图」与「字段完整性」面板
    #    直接消费这里，不必再自己把事实重新分章（口径统一在后端）。
    stats["by_chapter"] = _chapter_stats_for_items(all_items)

    return {
        "groups": page_groups,
        "stats": stats,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total_groups": total_groups,
            "returned": len(page_groups),
        },
    }


# ---------------------------------------------------------------------------
# 写接口（带缓存失效联动）
# ---------------------------------------------------------------------------

#: 列表项识别：单字符列表符 + 空白 + 正文（与 update_fact 的 multi_line 判定对齐）
_LIST_BULLET_RE = re.compile(r"^\s*[-*•+·]\s+(\S.*)$")
#: 行首是否为列表标记：update_fact 判定「多行列表 → 转分组重建」的口径。
#: ✅ BUG 修复（2026-09-21）：旧实现用 startswith(("-", "*", "•"))，只认三种，
#:    而「保存后重建」用的 _split_fact_lines 认的是完整集合——手写 "+ 项: 值" /
#:    "· 项: 值" 的多行分组编辑会被判为「非列表正文」而整体塞进单行 content，
#:    第 2..N 行永久丢失（静默数据丢失）。两处口径现已共用同一字符集。
_LIST_MARKER_PREFIX_RE = re.compile(r"^\s*[-*•+·]\s*(\S|$)")


def _looks_like_fact_line(text: str) -> bool:
    """content 是否为可回解的结构化事实行（列表符开头 或 含名:值冒号）。

    ✅ 与 update_fact 的行级 / 分组级分流配套：只有结构化行才走
    _apply_item_updates 的条目级口径（能同步 name / value / is_simulated）；
    非结构化的普通正文（如"基坑深度约 12.5 米"）回解不出 value，只能原样
    落到 content 列，否则会被退化成空值。
    """
    s = str(text or "").strip()
    if not s:
        return False
    return bool(_LIST_BULLET_RE.match(s)) or "：" in s or ":" in s


#: 纯列表符 / 水平分隔线（无任何正文）：不构成事实，直接跳过。
#: ✅ BUG 修复（2026-09-21）：旧口径只认「单个列表符 + 空白」与「≥3 个 -_ *」，
#:    "* *" / "- -" / "• •" 这类多列表符的噪音行会漏过，被当成正文解析成
#:    (name="*", value="") 的假事实，出现在事实列表里。
_LIST_NOISE_RE = re.compile(r"^\s*[-*•+·][-*•+·\s]*$")


def _split_fact_lines(title: str, content: str) -> list[tuple[str, str, bool]]:
    """把 Markdown 列表 content 拆为 (name, value, is_simulated) 行。

    - 列表行 "- **名**: 值" / "* **名**: 值" / "• **名**: 值" → 提取名称与值，保留模拟值标记
    - 非列表行 → (title, 整行, False)
    - 全部为空时回退为单行 (title, 整段 content, False)

    ✅ BUG 修复（2026-09-21）：旧实现只认 "-" 开头的列表项，而 update_fact 判定
    「是否走分组重建」用的是 ("-", "*", "•")——同一份 content 在两条路径口径不同。
    手工编辑保存后："* 补充说明" 被当「非列表正文」处理，name 被填成分组标题、
    value 带着原始 "*" 前缀入库（假事实）；"• **A**: 1" 更会把整行原文塞进 value；
    "-" 单独一行也会造出 value="-" 的空事实。现统一列表符识别，并在解析前剥掉
    列表符（extract_value_from_markdown_line 只会 lstrip("-")，不认 "*" / "•"）。
    """
    parsed: list[tuple[str, str, bool]] = []
    for line in (content or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if _LIST_NOISE_RE.match(line):
            # 纯 "-" / "* *" / "---" 之类无正文的行，不应当成事实
            continue
        m = _LIST_BULLET_RE.match(line)
        if m:
            body = m.group(1).strip()
            is_sim = is_simulated_marked(body)
            nm, val = extract_value_from_markdown_line(body)
            if nm or val:
                parsed.append((nm or title or "未命名", val, is_sim))
            else:
                parsed.append((title or "未命名", body, False))
        else:
            parsed.append((title or line[:30], line, False))
    if not parsed:
        parsed = [(title or "未命名", content or "", False)]
    return parsed


@router.get("/categories")
async def list_fact_categories():
    """事实分类清单（CATEGORY_TITLES 单一事实源）。

    ✅ 消三侧口径债（2026-09-20）：前端此前硬编码 FACT_CATEGORY_OPTIONS
    （仅 12 类，缺 deployment/process/quality 等 11 类），与后端 23 类
    双份维护。前端下拉改取本端点，本地常量仅作离线回退。
    """
    return {
        "categories": [
            {"value": k, "label": v} for k, v in CATEGORY_TITLES.items()
        ]
    }


@router.get("/category-map")
async def get_fact_category_map():
    """九大章节分类映射表（前端下拉 / 章节视图的单一事实源）。

    ✅ 消口径债：与 /categories 同理，九大章节的编码-中文名、事实属性枚举、
    数据来源枚举、category→chapter 映射全部由后端下发，前端不再硬编码。
    纯静态数据、无 DB 查询，供前端一次性拉取。
    """
    return category_map_payload()


@router.get("/chapters")
async def list_facts_by_chapters(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """九大章节视图：每章的事实清单 + 应提取字段覆盖率 + 缺失字段差集。

    建办质〔2018〕31号 规定专项方案分九大章节；本端点把已提取的全局事实
    按章节重组，并给出「该章节应提取哪些字段 / 已覆盖哪些 / 还缺哪些」，
    供用户在提取后快速定位缺口（而不是靠肉眼比对正文）。

    Returns:
        ``{"chapters": [{"chapter","key","title","count","coverage",
                         "missing_fields","items":[...]}], "totals": {...}}``
    """
    rows = await _load_fact_rows(db, scheme_id, project_id)
    enriched = []
    for r in rows:
        r = dict(r)
        # ✅ 字段完整性判定需要「事实名」，而落库的 title 列即事实名；
        #    content 列是 Markdown 行（"- **名**: 值"），拿它匹配字段名会全不命中。
        dims = _fact_dimension_fields(
            r, r.get("title") or "", r.get("title") or "",
            r.get("fact_key") or "",
            _parse_source_ref(r.get("source_ref", "") or ""))
        r.update(dims)
        enriched.append(r)
    summary = nine_chapter_summary(enriched)
    comp = chapter_field_completeness(enriched)

    # 把每条事实挂回它的章节（未分类的归入 __uncategorized，便于发现遗漏）
    by_chapter: dict[str, list[dict]] = {k: [] for k in CHAPTER_ORDER}
    uncategorized: list[dict] = []
    for r in enriched:
        # content 是 Markdown 行（"- **名**: 值"），此处解出干净的名称与取值
        _n, _v = extract_value_from_markdown_line(r.get("content") or "")
        item = {
            "fact_id": r["id"],
            "name": _n or r.get("title") or "",
            "value": _v or "",
            "category": r.get("category") or "",
            "chapter": r.get("chapter") or "",
            "chapter_title": r.get("chapter_title") or "",
            "fact_attr": r.get("fact_attr") or "",
            "source_kind": r.get("source_kind") or "",
            "is_shared": bool(r.get("is_shared")),
            "is_simulated": bool(r.get("is_simulated", 0)),
            "is_resolved": bool(r.get("is_resolved", 1)),
            "is_stale": bool(r.get("is_stale", 0)),
        }
        ch = r.get("chapter") or ""
        if ch in by_chapter:
            by_chapter[ch].append(item)
        else:
            uncategorized.append(item)

    chapters_out = []
    for ch_info in summary["chapters"]:
        key = ch_info["key"]
        cp = comp["chapters"][key]
        chapters_out.append({
            **ch_info,
            "fields": cp["fields"],
            "covered_fields": cp["covered_fields"],
            "missing_fields": cp["missing_fields"],
            "items": by_chapter.get(key, []),
        })

    return {
        "chapters": chapters_out,
        "uncategorized": uncategorized,
        "totals": summary["totals"],
        "by_fact_attr": _chapter_stats_for_items(enriched)["by_fact_attr"],
        "by_source_kind": _chapter_stats_for_items(enriched)["by_source_kind"],
    }


@router.post("/danger-check")
async def check_danger_scheme(
    data: dict,
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """专项方案类型自动识别 + 危大工程/超过一定规模 阈值判定。

    两步判定，均按确定性规则执行（不调用 AI，可解释、可复算）：
    1. **方案类型识别**：方案名称（或 ``extra_text`` 补充文本）关键词解析
       → 六大类危大工程（基坑/模板支撑/起重吊装/脚手架/拆除/其他）及子类；
    2. **阈值判定**：从全局事实中抽取定量参数（开挖深度/支撑高度/跨度/
       荷载/起重量等，长度类自动换算为米），再对照 HAZARD_THRESHOLDS
       → 是否危大 / 是否超过一定规模 / 命中的阈值条目 / 仍需补全的参数。

    本端点为**只读**诊断接口，不改动任何数据；阈值的单一事实源在
    ``app.services.scheme_classification``（与提取项目模块共用，不重复维护）。

    Returns:
        ``{"classification": {...}, "threshold_params": {...},
           "category_id": str, "missing_params": [str]}``
    """
    name = str((data or {}).get("scheme_name") or "").strip()
    extra = str((data or {}).get("extra_text") or "").strip()
    # ✅ P0 修复（2026-10-01）：400 检查此前位于「从库中取方案名」**之前** ——
    #    前端工作台「事实诊断」面板只传 query 的 scheme_id（body 为空对象），
    #    name/extra 均为空 → 恒 400「需要 scheme_name 或 extra_text」，
    #    下方「从库中取方案名」的分支永远不可达（注释与实现矛盾）。
    #    生产实证：backend_err.log 多次 400，同方案同时段 /chapters 为 200、
    #    /danger-check 恒 400。现把「取方案名」提前；400 仅在
    #    「scheme_name/extra_text 均空 且 scheme_id 也取不到方案名」时拒绝
    #    （全空调用方仍合理 400，不做无依据判定）。
    if not name and scheme_id:
        cur = await db.execute("SELECT name FROM schemes WHERE id=?", (scheme_id,))
        row = await cur.fetchone()
        if row:
            name = row["name"] or ""
    if not name and not extra:
        raise HTTPException(400, "需要 scheme_name 或 extra_text")
    # ✅ P0 修复（2026-09-27 · 危大阈值判定用编造值/过期值算）：
    #    「超过一定规模」是给监管看的**确定性结论**，绝不能用 AI 编造的模拟值
    #    （is_simulated=1，待确认）或已被重新提取取代的过期值（is_stale=1）
    #    来判定 —— 误判会在前端红标展示，性质上比不判更糟。
    #    injectable_only 复用 facts_extractor._FACTS_INJECT_WHERE，
    #    与正文注入、导出门控完全同一口径。
    rows = await _load_fact_rows(db, scheme_id, project_id,
                                 injectable_only=True)
    # ✅ 抽取参数用的是「事实名 + 取值」，而落库列是 title / content（Markdown 行）。
    #    直接用原始行会让 name/value 恒为空 → 参数抽不出、阈值判定退化为缺参。
    facts = []
    for r in rows:
        n, v = extract_value_from_markdown_line(r.get("content") or "")
        facts.append({
            "name": n or r.get("title") or "",
            "value": v or "",
            "value_unit": r.get("value_unit") or "",
            "fact_key": r.get("fact_key") or "",
        })
    params = extract_danger_params(facts)
    result = scheme_clf.classify_scheme(name, params, extra)
    category_id = (result.category_ids[0] if result.category_ids else "")
    # 缺失参数聚合自各命中子类的阈值判定（missing_params 不在聚合对象顶层）
    missing: list[str] = []
    for h in (result.hazards or []):
        for p in (h.get("missing_params") or []):
            if p and p not in missing:
                missing.append(p)
    return {
        "classification": result.to_dict(),
        "threshold_params": params,
        "category_id": category_id,
        "missing_params": missing,
    }


@router.post("")
async def create_fact(
    data: FactGroupIn,
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """创建事实分组（兼容旧版 + 新版结构化 items）"""
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    # scheme_id 为空时保存为项目级事实；有 scheme_id 时使用数据库归属的项目 ID。
    # ✅ 同 _resolve_project_id：非字符串（如未解析的 Query 默认值）必须按空处理，
    #    否则会把 "Query('')" 当成真实 scheme_id 落库，产生永远查不到的幽灵作用域。
    scheme_scope = scheme_id.strip() if isinstance(scheme_id, str) else ""
    gid = str(uuid.uuid4())
    # ✅ 归类兜底为 other（与提取/编辑一致），避免空类别导致排序/展示异常
    category = (data.category or "").strip() or "other"
    # ✅ 修复：分组标题此前未落库，重查后分组名退化为单条事实名。
    #    用户输入的分组标题优先，其次类别中文名。
    group_title = (data.title or "").strip() or CATEGORY_TITLES.get(category, "")

    # ✅ G-04 修复（2026-10-04）：executemany + commit 是**多语句**写入路径。
    #    executemany 分批下推时若中途失败（某行占位符列数不匹配、DB 锁超时、
    #    JSON 序列化异常），已下推的部分行留在悬空事务里；连接归还池后会被
    #    下个请求的 commit 连带提交，形成「部分事实入库 + 前端拿到 500」的脏状态。
    #    与 update_fact(:1562)、clear_all_facts(:1857) 同口径显式回滚，避免脏状态。
    try:
        rows_buf = _build_create_fact_rows(
            data=data, gid=gid, group_title=group_title, category=category,
            pid=real_pid, sid=scheme_scope)
        if rows_buf:
            await db.executemany(MANUAL_FACT_INSERT_SQL, rows_buf)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    # ✅ BUG 修复（2026-09-29 · 项目级新事实不失效导出缓存）：
    # 旧实现只在 scheme_scope 非空时失效单个方案缓存 —— 项目级事实
    # （scheme_id 为空，供项目下全部方案共用）落库后**一个缓存都不失效**，
    # 用户新建「合同工期」等事实后导出仍用旧缓存，产物缺该事实却查不出原因。
    # 现与 update_fact 统一走 _invalidate_fact_scope_cache（按项目覆盖全部方案）。
    await _invalidate_fact_scope_cache(db, scheme_scope, str(real_pid))
    return {"id": gid, "ok": True}


def _build_fact_content(name: str, value: str, is_simulated: bool) -> str:
    """构造事实行的 Markdown content（模拟值标记后置，供列表接口回解）

    ✅ BUG 修复（2026-09-21）：标记改用 facts_extractor.append_simulated_marker
    单一口径（落库 / SSE 下发 / 回解三侧共用），并在写入前先剥离旧标记，
    避免同一行叠加两个模拟值标记。
    """
    return f"- **{name}**: {append_simulated_marker(value, is_simulated)}"


#: ✅ BUG 修复（2026-10-01 · 手工事实 INSERT 列清单三处各自维护）：
#: 「手工 / AI 调整新增事实」此前有**三份**硬编码 INSERT —— create_fact 的
#: 结构化分支、create_fact 的旧版单条分支、adjust_facts 的 add 分支，列名与
#: 占位符各写一遍。这与 2026-09-29 分组重建「27 个 ? vs 28 列」静默错位是
#: 同一类陷阱：任何一处给 global_facts 加列（九大章节四维、溯源列、
#: is_safety_critical…）都容易只改一处、漏两处，新增路径写进去的行就永远
#: 是默认值，而读路径靠惰性派生「看起来正常」，问题难以发现。
#: 现把列清单收敛为单一事实源，占位符由列数派生（不再手写）。
#: ✅ G-05 修复（2026-10-04 · 列清单漂移单一事实源）：
#: 旧实现里 MANUAL_FACT_INSERT_COLS（15 列）与 _GROUP_REBUILD_COLS（28 列）
#: 各自硬编码前 15 列 + 溯源/四维的追加列 —— 任何一次 schema 加列都必须
#: 同步改两处，一处漏改即「分组重建写入的字段在手工/AI 新增路径永远回落到
#: 默认值」，静默漂移、难以发现。现把 28 列全部收敛到 MANUAL_FACT_INSERT_COLS，
#: _GROUP_REBUILD_COLS 直接引用它作为别名，两条 INSERT 路径共用同一事实源。
#: 新增路径对额外 13 列（溯源/单位/九大章节四维）显式写默认值，等价于旧的
#: 「不写入该列 → 走表默认值」，schema 中每列都自带 DEFAULT，行为完全一致。
MANUAL_FACT_INSERT_COLS = (
    "id", "project_id", "scheme_id", "group_id", "group_title", "title", "content",
    "category", "source_ref", "is_simulated", "confidence", "is_resolved",
    "has_conflict", "conflict_keys", "fact_key",
    # 溯源/单位列（提取管线写入；手工新增走默认值）
    "chunk_hash", "value_unit", "fact_type", "evidence_kind", "page_ref",
    "zone_type", "is_safety_critical", "norm_group", "is_stale",
    # 九大章节四维标注（apply_fact_dimensions 派生；手工新增走默认值）
    "chapter", "fact_attr", "source_kind", "is_shared",
)
MANUAL_FACT_INSERT_SQL = "INSERT INTO global_facts (%s) VALUES (%s)" % (
    ", ".join(MANUAL_FACT_INSERT_COLS),
    ",".join("?" for _ in MANUAL_FACT_INSERT_COLS))


def _manual_fact_row(*, fid: str, pid: str, sid: str, group_id: str,
                     group_title: str, name: str, content: str, category: str,
                     source_file: str, source_quote: str = "",
                     is_simulated: bool = False, confidence: float = 1.0,
                     is_resolved: bool = True, fact_key: str = "") -> tuple:
    """构造「手工 / AI 调整新增」事实行（列顺序与 MANUAL_FACT_INSERT_COLS 严格对齐）。

    三处新增路径共用，杜绝「列名与占位符数量错位」与「漏写某一列」。
    追加的 13 列（溯源/单位/九大章节四维）显式写默认值，与旧行为完全一致
    （schema 中每列都带 DEFAULT，读路径的惰性派生兜底不受影响）。

    ``fact_key`` 允许调用方显式指定（客户端传入的归一化键优先），
    缺省按事实名归一化生成去重键。
    """
    return (
        fid, pid, sid, group_id, group_title,
        name, content,
        # 归类兜底 other（与提取/编辑一致），避免空类别导致排序/展示异常
        (str(category or "").strip() or "other"),
        json.dumps([{"file": source_file,
                     # 与提取落库同口径：超长截断并补省略号，不静默截断
                     "quote": _clip_excerpt(source_quote or "", MAX_SOURCE_EXCERPT)}],
                   ensure_ascii=False),
        1 if is_simulated else 0,
        _safe_confidence(confidence, 1.0),
        int(is_resolved),
        0, "",  # has_conflict / conflict_keys：新增事实无矛盾候选
        (str(fact_key or "").strip() or normalize_key(name)),
        # 溯源/单位列默认值（手工/AI 新增无 chunk 指纹、无页码、非安全关键、非过期）
        "", "", "", "", None, "", 0, "", 0,
        # 九大章节四维默认值（读路径 apply_fact_dimensions 惰性派生兜底）
        "", "", "", 0,
    )


def _build_create_fact_rows(*, data, gid: str, group_title: str,
                            category: str, pid: str, sid: str) -> list[tuple]:
    """把 FactGroupIn 展开为待 INSERT 的行元组列表（列顺序与 MANUAL_FACT_INSERT_COLS 对齐）。

    ✅ G-04 重构（2026-10-04）：从 create_fact 内联抽出，使 create_fact 的主
    写入路径整体包在 try/except 事务守卫里 —— 行构造阶段的任何异常（含
    _manual_fact_row 内的 JSON 序列化）与 executemany/commit 的异常走同一回滚
    口径，避免「行缓冲构造失败 + commit 已被调用」或反之的脏状态。
    两条分支保留原有语义：结构化 items 逐条入库；旧版单条按多行 Markdown 拆开。
    """
    if data.items:
        rows_buf: list[tuple] = []
        for it in data.items:
            fid = str(uuid.uuid4())
            content = _build_fact_content(it.name, it.value, it.is_simulated)
            # ✅ BUG 修复（2026-09-21）：模拟值闸门是**不变式**而非冗余标记。
            #    注入 / 导出门控是 `has_conflict=0 AND is_resolved=1`（不含
            #    is_simulated 列），全靠 `is_simulated=1 ⟹ is_resolved=0` 维持。
            #    旧实现按模型默认值 is_resolved=True 直落库，客户端只传
            #    is_simulated 不传 is_resolved 时（模型默认 True），编造值会
            #    带着 is_resolved=1 直接越过闸门注入正文与导出。与
            #    _apply_item_updates 的既有口径对齐。
            item_resolved = 1 if (it.is_resolved and not it.is_simulated) else 0
            item_cat = it.category or category
            # ✅ BUG 修复：无来源时也必须写入"手动录入"标记——persist_extraction
            #    的 _is_protected 靠 source_ref 识别手动来源保护未确认事实，
            #    旧实现 source/source_ref 皆空时留空 → 手动新增但未确认的事实
            #    会在「重新提取」时被当旧 AI 数据删除。
            #    （quote 的超长截断口径见 _manual_fact_row → _clip_excerpt）
            # ✅ BUG 修复（2026-10-01）：列清单与取值改走 _manual_fact_row
            #    单一出口（原为手写 15 列 INSERT，与另两处新增路径各自维护）。
            rows_buf.append(_manual_fact_row(
                fid=fid, pid=pid, sid=sid,
                group_id=gid, group_title=group_title,
                name=it.name, content=content, category=item_cat,
                source_file=it.source or "手动录入",
                source_quote=it.source_ref or "",
                is_simulated=it.is_simulated,
                confidence=it.confidence,
                is_resolved=bool(item_resolved),
                fact_key=it.key or "",
            ))
        return rows_buf

    # 旧版单条：title + content
    # ✅ 修复：content 常为多行 Markdown 列表（前端「手动新增」弹窗多行输入），
    #    原实现整块存为一行 → 列表接口只解析出第一行，其余行"凭空消失"。
    #    现与分组编辑（PATCH）一致，逐行拆开入库。
    rows_buf = []
    for nm, val, is_sim in _split_fact_lines(data.title, data.content):
        rows_buf.append(_manual_fact_row(
            fid=str(uuid.uuid4()), pid=pid, sid=sid,
            group_id=gid, group_title=group_title,
            name=nm, content=_build_fact_content(nm, val, is_sim),
            category=category,
            # ✅ BUG 修复：旧版单条路径同样必须写入"手动录入"来源标记，
            # 否则未确认的手动事实会被「重新提取」误删（同上）
            source_file="手动录入",
            is_simulated=is_sim,
            confidence=1.0,
            # ✅ BUG 修复（2026-09-21）：is_simulated ⟹ is_resolved=0（模拟值闸门
            #    是不变式，门控 SQL 只判 is_resolved）。旧实现恒写 is_resolved=1，
            #    粘贴了模拟值标记的手工分组会立刻被当确定性事实注入正文。
            is_resolved=(not is_sim),
        ))
    return rows_buf


async def _invalidate_item_update_caches(db, updates: list[dict]) -> None:
    """按条目实际作用域失效缓存；项目共享事实变更影响项目下所有方案。"""
    ids = [str(u.get("fact_id") or "").strip() for u in updates if u.get("fact_id")]
    if not ids:
        return
    ph = ",".join("?" for _ in ids)
    cur = await db.execute(
        f"SELECT DISTINCT project_id, scheme_id FROM global_facts WHERE id IN ({ph})", ids)
    for r in await cur.fetchall():
        await _invalidate_fact_scope_cache(
            db, str(r["scheme_id"] or ""), str(r["project_id"] or ""))


async def _apply_item_updates(db, updates: list[dict]) -> tuple[int, str]:
    """按 fact_id 逐条更新单条事实（name/value/category/is_simulated/confidence）。

    ✅ 修复（死字段 + 能力缺失）：FactGroupUpdate.item_updates 在模型中早已
    声明，后端却从未读取 —— 前端只能「整组重建」来改一条事实，代价是
    组内其它行的溯源/矛盾标记被一并重置。现提供真正的条目级更新。

    返回 (更新条数, scheme_id)。
    """
    # ✅ 性能修复：旧实现每条 update 各执行一次 SELECT（N+1 查询），
    # 批量保存大分组（数十条）时数据库往返线性放大。先一次 SELECT IN 取全。
    fids: list[str] = []
    seen: set[str] = set()
    for u in updates:
        fid = str(u.get("fact_id") or "").strip()
        if fid and fid not in seen:
            seen.add(fid)
            fids.append(fid)
    if not fids:
        return 0, ""
    placeholders = ",".join("?" for _ in fids)
    cur = await db.execute(
        f"SELECT id, title, content, category, is_simulated, confidence, scheme_id, "
        # ✅ 2026-09-29：fact_type 参与九大章节归属的派生（分类变更时重算 chapter），
        #    需一并预取，避免额外一次往返。
        f"fact_key, fact_type FROM global_facts WHERE id IN ({placeholders})", fids)
    rows = {r["id"]: r for r in await cur.fetchall()}

    scheme_id = ""
    updated = 0
    for u in updates:
        fid = str(u.get("fact_id") or "").strip()
        row = rows.get(fid)
        if not row:
            continue
        scheme_id = scheme_id or (row["scheme_id"] or "")

        sets: list[str] = []
        vals: list = []
        title = str(row["title"] or "")
        is_sim = bool(row["is_simulated"])
        title_changed = False
        value_changed = False

        raw_name = u.get("name")
        # 改名会替换归一化键；先取出旧键，改完后用新键参与章节派生
        new_key = (row["fact_key"] or "").strip()
        if raw_name is not None and str(raw_name).strip():
            title = str(raw_name).strip()
            title_changed = title != (str(row["title"] or ""))
            if title_changed:
                sets.append("title=?")
                vals.append(title)
                # ✅ BUG 修复：改名后 fact_key 不重算 —— 键仍指向旧名的归一化结果，
                # 重新提取时按新名生成的键无法与已确认事实去重，导致同一事实重复入库。
                new_key = normalize_key(title) or new_key
                if new_key and new_key != (row["fact_key"] or "").strip():
                    sets.append("fact_key=?")
                    vals.append(new_key)

        if u.get("is_simulated") is not None:
            is_sim = bool(u.get("is_simulated"))
            sets.append("is_simulated=?")
            vals.append(1 if is_sim else 0)
            # ✅ 模拟值闸门：标为模拟值即回到「待审核」，防止模拟值直接注入正文
            if is_sim:
                sets.append("is_resolved=?")
                vals.append(0)

        old_value = extract_value_from_markdown_line(row["content"] or "")[1]

        if u.get("value") is not None:
            new_value = str(u.get("value"))
            sets.append("content=?")
            vals.append(_build_fact_content(title, new_value, is_sim))
            # ✅ BUG 修复（2026-09-18）：人工改值即为对矛盾的裁决，必须同时清除
            #    冲突标记 —— 否则 has_conflict 仍为 1，该事实继续被注入门控
            #    （has_conflict=0 AND is_resolved=1）排除、界面也仍显示「存在矛盾」，
            #    用户被迫改用「分组编辑」或「选此值」。与分组重建路径（清
            #    has_conflict + conflict_keys）保持同一口径。
            # ✅ BUG 修复（2026-09-20）：但【仅在值真正变化时】才清除——前端
            #    单条编辑弹窗总是全量提交 value，旧实现无条件清冲突标记，
            #    导致「只改分类/改名/切换模拟值」等操作静默吞掉未裁决矛盾。
            #    比较口径与 persist_extraction 的冲突登记口径一致：
            #    strip 后文本不同且 normalize_key 不同才视为改值。
            value_changed = (
                old_value.strip() != new_value.strip()
                and normalize_key(old_value) != normalize_key(new_value)
            )
            if value_changed:
                sets.append("has_conflict=?")
                vals.append(0)
                sets.append("conflict_keys=?")
                vals.append("")
        elif title_changed:
            # 改名但没改值 → 同步 content 中的名称，避免列表回解出旧名
            sets.append("content=?")
            vals.append(_build_fact_content(title, old_value, is_sim))

        cat_changed = False
        new_cat = ""
        if u.get("category"):
            new_cat = str(u["category"]).strip()
            cat_changed = bool(new_cat) and new_cat != (row["category"] or "").strip()
            sets.append("category=?")
            vals.append(new_cat)
        # ✅ BUG 修复（2026-09-29 · 改分类后九大章节归属过期）+
        #    ✅ BUG 修复（2026-10-01 · 改名后九大章节归属过期）：
        #    chapter / fact_attr 的派生输入是
        #    (name, value, category, fact_type, fact_key)，而读路径
        #    （list_facts 的 _fact_dimension_fields、正文注入的
        #    _load_facts_rows）都是「库值优先」—— 派生输入变了但列不跟着
        #    重算，九大章节视图 / 正文按章精选就永远停在旧值，
        #    直到下一次重新提取才能纠正。
        #    2026-09-29 只堵住了「改分类」这一路；【改名】同样改变了派生
        #    输入（文本规则与归一化键都会变），却是漏改点：把「混凝土
        #    强度等级」改成「混凝土浇筑工艺」后，chapter 仍停在 technique。
        #    重派生必须独立于「是否提交了 category」—— 否则只改名不改类的
        #    单条编辑依旧不重算。
        if cat_changed or title_changed:
            _name_now = (title or (row["title"] or "")).strip()
            _val_now = (str(u["value"]).strip() if u.get("value") is not None
                        else str(old_value).strip())
            # ⚠️ row 是 sqlite3.Row（无 .get），必须按键取值
            _ft = (row["fact_type"] or "").strip()
            _cat_now = new_cat if cat_changed else (row["category"] or "")
            # 与 facts_classification.classify_fact_dimensions 同口径：
            # chapter 用 (name, value, category, fact_type, fact_key)，
            # fact_attr 只用 (name, value) —— 属性判定与 category 正交
            # （classify_fact_attr 已清理死参数，不接受 category）。
            sets.append("chapter=?")
            vals.append(classify_chapter_from_text(
                _name_now, _val_now, _cat_now, _ft, new_key))
            sets.append("fact_attr=?")
            vals.append(classify_fact_attr(_name_now, _val_now))

        if u.get("confidence") is not None:
            sets.append("confidence=?")
            vals.append(_safe_confidence(u.get("confidence"), 1.0))

        if not sets:
            continue
        # 来源已变化的事实只有在人工真正改值后才解除 stale；仅改分类/名称不能
        # 伪装成已核对。冲突裁决走 resolve_conflict 的专用写路径。
        if value_changed:
            sets.append("is_stale=0")
        # ✅ 统一时间戳口径：与分组重建一致使用 SQLite datetime 函数
        sets.append("updated_at=datetime('now','localtime')")
        await db.execute(
            f"UPDATE global_facts SET {', '.join(sets)} WHERE id=?", (*vals, fid))
        updated += 1

    if updated:
        await db.commit()
    return updated, scheme_id


# ✅ 2026-09-29：分组重建的列名与占位符**同源生成**。
# 手写 VALUES 占位符曾出现「27 个 ? vs 28 列」的静默错位 —— SQLite 直接抛
# OperationalError 中断整次分组编辑，用户改动全部丢失且报错信息毫无指引。
def _value_signature(value: str) -> str:
    """事实值签名：取首个数字串（含小数），用于同名事实的跨形态配对。

    用户编辑分组时常给值补单位（库内「12.5」→ 提交「12.5 m」），此时 strip
    后的精确比对失败。数字串是两个同名事实最强的区分特征，足以安全配对；
    非数字值（如「每日一次」）签名返回空串，调用方按顺序兜底。
    """
    m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return m.group(0) if m else ""


#: ✅ G-05 修复（2026-10-04）：分组重建路径与手工/AI 新增路径共用
#: MANUAL_FACT_INSERT_COLS 单一事实源，杜绝两侧漂移。_GROUP_REBUILD_SQL 与
#: MANUAL_FACT_INSERT_SQL 完全等价 —— 保留此别名仅为语义清晰（分组重建的
#: 调用点可读性更好），后续若两处再次分叉会由测试兜住。
_GROUP_REBUILD_COLS = MANUAL_FACT_INSERT_COLS
_GROUP_REBUILD_SQL = MANUAL_FACT_INSERT_SQL


@router.patch("/{fact_id}")
async def update_fact(
    fact_id: str,
    data: FactGroupUpdate,
    scheme_id: str = Query(""),
    db=Depends(get_db),
):
    """更新事实（支持字段级更新 + 条目级更新 + 缓存失效联动）

    fact_id 兼容两种模式：
    1. 行 id（单条事实行）→ 原地字段更新
    2. group_id（分组编辑，前端编辑弹窗传 group.id）→ 按编辑后的
       content（Markdown 列表）重建该分组，保留模拟值/矛盾标记

    另支持 data.item_updates：按 fact_id 逐条更新，不重建分组。
    """
    fields = {k: v for k, v in data.model_dump(exclude_none=True).items()
              if k not in ("id", "item_updates")}
    item_updates = [u for u in (data.item_updates or [])
                    if isinstance(u, dict) and u.get("fact_id")]

    scheme_scope = scheme_id.strip() if isinstance(scheme_id, str) else ""

    # ---- 模式 0：条目级更新（可独立使用，也可与分组字段同时提交）----
    if scheme_scope:
        for u in item_updates:
            await _assert_fact_in_scheme_scope(db, str(u.get("fact_id") or ""), scheme_scope)
        await _assert_fact_in_scheme_scope(db, fact_id, scheme_scope)
    if item_updates:
        n, sid = await _apply_item_updates(db, item_updates)
        if not fields:
            await _invalidate_item_update_caches(db, item_updates)
            return {"ok": True, "updated_items": n}

    cur = await db.execute(
        "SELECT * FROM global_facts WHERE id=?", (fact_id,))
    row = await cur.fetchone()

    # ✅ BUG 修复：行级更新收到【多行 Markdown content】时（前端把整组内容
    #    提交到某一行，或用户粘贴多行列表），旧实现把整块文本塞进单行 content，
    #    列表接口只解析首行 → 第 2..N 行永久丢失（静默数据丢失）。
    #    现识别多行列表并转走「分组重建」路径。
    # ✅ 加固（2026-09-21）：旧判定 `count("-") >= 1` 过宽 —— 只要全文任意位置
    #    出现一个连字符（如日期区间 "2024-2025"）就把普通多行文本误判为列表，
    #    走「分组重建」（DELETE+INSERT，行 id 全换），前端持有旧行 id 的后续
    #    编辑会 404。现收紧为「至少一个非空行以列表标记开头」，与
    #    _split_fact_lines 的解析口径对齐。
    _content_val = str(fields.get("content") or "")
    multi_line = "\n" in _content_val.strip() and any(
        _LIST_MARKER_PREFIX_RE.match(ln.strip())
        for ln in _content_val.splitlines() if ln.strip())

    if row and not multi_line:
        # ---- 模式 1：行级更新 ----
        # ✅ BUG 修复（2026-09-21）：结构化的 title / content 不再裸写列，改走
        #    _apply_item_updates 的条目级口径。旧实现三处失同步：
        #      1) 改 title 只写 title 列，content 里的「**旧名**」不跟着改、
        #         fact_key 不重新归一化 → 列表接口按 content 回解，界面永远显示
        #         旧名；重新提取也按旧键去重，同一事实重复入库；
        #      2) 改 content 为模拟值文本，is_simulated / is_resolved 不动 →
        #         模拟值闸门失效，编造值被当确定性事实注入正文与导出；
        #      3) 改值不清 has_conflict（条目级口径会清）→ 与分组重建路径漂移。
        #    非结构化的普通正文（无列表符、无冒号）回解不出 name/value，
        #    仍走原地更新，避免被退化成空值。
        _structured = bool(fields) and (
            "title" in fields or _looks_like_fact_line(fields.get("content")))
        if _structured:
            iu: dict = {"fact_id": fact_id}
            if "content" in fields and _looks_like_fact_line(fields.get("content")):
                nm, val = extract_value_from_markdown_line(str(fields["content"] or ""))
                iu["value"] = val
                # ✅ 从 content 回解模拟值标记（历史两种写法通吃），与 is_simulated 列对齐
                iu["is_simulated"] = is_simulated_marked(str(fields["content"] or ""))
                if nm and nm != str(row["title"] or ""):
                    iu["name"] = nm
            if "title" in fields and str(fields.get("title") or "").strip():
                iu["name"] = str(fields["title"]).strip()
            if fields.get("category"):
                iu["category"] = str(fields["category"]).strip()
            if len(iu) > 1:
                n, _sid = await _apply_item_updates(db, [iu])
                await db.commit()
                # ✅ 2026-09-29：项目级事实（scheme_id 为空）此前不失效任何缓存，
                #    统一走 _invalidate_fact_scope_cache（按项目覆盖全部方案）。
                await _invalidate_fact_scope_cache(
                    db, str(row["scheme_id"] or ""), str(row["project_id"] or ""))
                return {"ok": True, "updated_items": n}
        if fields:
            # ✅ 统一时间戳口径：与其它写接口一致使用 SQLite datetime 函数。
            #    旧实现用 datetime.now().isoformat()（'T' 分隔），与
            #    'YYYY-MM-DD HH:MM:SS' 混排导致 updated_at 排序错乱。
            sets = ", ".join(f"{k}=?" for k in fields)
            await db.execute(
                f"UPDATE global_facts SET {sets}, "
                f"updated_at=datetime('now','localtime') WHERE id=?",
                (*fields.values(), fact_id))
        await db.commit()
        # ✅ 2026-09-29：同上，项目级事实也必须失效项目下全部方案的导出缓存
        # （旧实现只判 scheme_id 非空，项目级行一个缓存都不失效）。
        await _invalidate_fact_scope_cache(
            db, str(row["scheme_id"] or ""), str(row["project_id"] or ""))
        return {"ok": True}

    # ---- 模式 2：分组重建 ----
    group_id = (row["group_id"] if row else None) or fact_id
    # ✅ BUG 修复（2026-10-03 · 作用域不对称）：group_id 不是全局唯一约束
    #    （delete_fact :2030 已明确此风险并用作用域限定修复），但本路径查旧行
    #    从未限定 —— 历史数据/旧版复制方案残留的跨方案重复 group_id 场景下，
    #    `grow = old_rows[0]` 可能取到**其它方案**的行：新行插到错误作用域、
    #    DELETE 按错误作用域执行 → 本分组旧行未删（悬空重复）+ 改动越域。
    #    与 delete_fact 同口径：fact_id 命中真实行时，按其 project/scheme 限定。
    if row:
        cur = await db.execute(
            "SELECT * FROM global_facts WHERE group_id=? AND project_id=? "
            "AND COALESCE(scheme_id,'')=?",
            (group_id, str(row["project_id"] or ""), str(row["scheme_id"] or "")))
    else:
        cur = await db.execute(
            "SELECT * FROM global_facts WHERE group_id=?", (group_id,))
    old_rows = [dict(r) for r in await cur.fetchall()]
    if not old_rows:
        raise HTTPException(404, "事实不存在")
    grow = old_rows[0]

    title = fields.get("title", "") or ""
    content = fields.get("content", "") or ""
    # ✅ BUG 修复：前端编辑分组未回传 category 时沿用原分组类别；
    #    旧实现取空串后落库为 "other"，编辑一次即丢失归类。
    category = ((fields.get("category") or "").strip()
                or (grow.get("category") or "").strip() or "other")
    # ✅ 修复：分组标题需落到 group_title 列，否则重查后分组名退化。
    group_title = title.strip() or CATEGORY_TITLES.get(category, "") or "其他事实"

    # ✅ BUG 修复：重建前按名称索引旧行，保留溯源(source_ref)/归一化键(fact_key)/
    #    置信度。旧实现重建时把这些字段一律清空 —— 编辑一次分组即丢失来源引用，
    #    且 fact_key 丢失会使后续「重新提取」无法与已确认事实去重而重复入库。
    # ✅ BUG 修复（2026-09-29 · 同名事实覆盖）：旧实现用 dict 推导按 title 建
    #    索引，同一分组内出现两条同名事实（不同值，如招标文件 12.5m 与补充通知
    #    14.0m）时**后写覆盖先写** → 第一条的 source_ref / fact_key / confidence /
    #    页码溯源被第二条整条顶掉。现按 (title, value) 精确匹配优先、未命中回退
    #    首个同名行，任一旧行都不会被吞。
    meta_by_name: dict[str, list[dict]] = {}
    for r in old_rows:
        meta_by_name.setdefault((r.get("title") or "").strip(), []).append(r)

    def _lookup_old(nm: str, val: str) -> dict | None:
        """按名称取旧行；同名多条时按值精确配对，命中即弹出避免二次配对。

        三级兜底，任何一级都不会让两条新事实指向同一条旧行：
        1. 值 strip 后完全一致（常见情形：用户未改动值）；
        2. 数字签名一致（用户补了单位：旧「12.5」/ 新「12.5 m」）；
        3. 按入库顺序取尚未配对的下一条（保序配对，优于「两条都取第一条」）。
        """
        cands = meta_by_name.get((nm or "").strip())
        if not cands:
            return None
        v_norm = str(val or "").strip()
        if v_norm:
            for i, c in enumerate(cands):
                old_v = str(extract_value_from_markdown_line(
                    c.get("content") or "")[1] or "").strip()
                if old_v == v_norm:
                    return cands.pop(i)
            vsig = _value_signature(v_norm)
            if vsig:
                for i, c in enumerate(cands):
                    old_v = str(extract_value_from_markdown_line(
                        c.get("content") or "")[1] or "")
                    if _value_signature(old_v) == vsig:
                        return cands.pop(i)
        return cands.pop(0)

    def _carry_provenance(old: dict | None) -> tuple:
        """重建后原样携带旧行的溯源/单位列（与 to_db_row 落库集合对齐）。

        ✅ BUG 修复（2026-09-29 · 静默数据丢失）：旧 INSERT 只写 15 列，
        未列入的列回落到 DB 默认值（''/NULL/0）—— 一次分组编辑即永久丢失
        来源页码(page_ref)、计量单位(value_unit)、证据类型(evidence_kind)、
        语义区(zone_type)、归一化组(norm_group)、增量指纹(chunk_hash)、
        安全关键标记(is_safety_critical) 与来源过期标记(is_stale)。
        其中 chunk_hash 丢失还会让 persist_extraction 的「跳过段事实保留」
        判据失效（chunk_hash 为空即视为「无指纹」而纳入删除范围）。
        """
        if not old:
            return ("", "", "", "", None, "", 0, "", 0)
        return (
            old.get("chunk_hash") or "",
            old.get("value_unit") or "",
            old.get("fact_type") or "",
            old.get("evidence_kind") or "",
            old.get("page_ref"),
            old.get("zone_type") or "",
            int(old.get("is_safety_critical") or 0),
            old.get("norm_group") or "",
            int(old.get("is_stale") or 0),
        )

    def _carry_dimensions(old: dict | None, nm: str, val: str) -> tuple:
        """重建后携带九大章节四维标注；派生输入变化时重派生 chapter/fact_attr。

        口径与提取管线 apply_fact_dimensions 一致（确定性规则）；派生输入
        **全部未变**时原样保留库值（尊重提取期标注），避免重新提取才能刷新章节归属。

        ✅ BUG 修复（2026-10-01 · 改名后九大章节归属过期）：旧实现只比较
        **分类**是否变化——分组内某条事实改名（如「混凝土强度等级」→「混凝土
        浇筑工艺」）而分类未变时，chapter / fact_attr 原样保留旧值，而读路径
        「库值优先」会把旧值当权威 → 九大章节视图 / 正文按章精选永久错配，
        直到下一次重新提取才能纠正。与条目级更新（_apply_item_updates）同口径：
        name 变化同样是派生输入变化，必须重派生。
        """
        if not old:
            return ("", "", "", 0)
        old_cat = ((old.get("category") or "").strip() or "other")
        old_name = str(old.get("title") or old.get("name") or "").strip()
        if old_cat == ((category or "").strip() or "other") \
                and old_name == str(nm or "").strip():
            return (old.get("chapter") or "", old.get("fact_attr") or "",
                    old.get("source_kind") or "",
                    int(old.get("is_shared") or 0))
        return (
            classify_chapter_from_text(nm, val, category,
                                       old.get("fact_type") or "",
                                       (old.get("fact_key") or "").strip()),
            classify_fact_attr(nm, val),
            old.get("source_kind") or "",
            int(old.get("is_shared") or 0),
        )

    # 解析 Markdown 列表行为 (name, value, is_simulated)
    parsed: list[tuple[str, str, bool]] = _split_fact_lines(title, content)

    # 注：DELETE 延迟到 insert_buf 构建完成后执行，与 INSERT+commit 同事务包裹
    # （防止中途异常导致悬空 DELETE 被后续请求连带提交、整组静默丢失）。

    insert_buf = []
    for nm, val, is_sim in parsed:
        content_line = _build_fact_content(nm, val, is_sim)
        # ✅ BUG 修复：模拟值行必须保持 is_resolved=0（模拟值闸门生效）；
        # 非模拟值行才是用户主动编辑后视为已确认（is_resolved=1）。
        is_resolved = 0 if is_sim else 1
        old = _lookup_old(nm, val)
        source_ref = (old.get("source_ref") if old else "") or ""
        fact_key = (old.get("fact_key") if old else "") or normalize_key(nm)
        # 人工编辑保存的非模拟值视为已确认 → 置信度拉满；
        # 模拟值保留原置信度（仍待审核，受闸门约束）。
        confidence = 1.0
        if is_sim:
            confidence = (_safe_confidence(old.get("confidence"), 0.8)
                          if old else 0.8)
        # ✅ BUG 修复：旧实现无条件把 has_conflict / conflict_keys 清零，
        #    用户只是改了分组标题或调整了某一行的措辞，整组的矛盾候选值就
        #    全部消失，未裁决的矛盾再也无法在界面上找回（只能重新提取）。
        #    现仅当该行内容确实被改动时才清除矛盾；内容未变则原样保留。
        has_conflict, conflict_keys = 0, ""
        if old:
            if (old.get("content") or "").strip() == content_line.strip():
                has_conflict = int(old.get("has_conflict") or 0)
                conflict_keys = old.get("conflict_keys") or ""
        insert_buf.append((
            str(uuid.uuid4()), grow["project_id"], grow["scheme_id"],
            group_id, group_title, nm, content_line, category,
            source_ref, 1 if is_sim else 0, confidence, is_resolved,
            has_conflict, conflict_keys, fact_key)
            + _carry_provenance(old) + _carry_dimensions(old, nm, val))
    # content 不是列表（旧版纯文本）→ 整块存为单行
    if not insert_buf:
        old = _lookup_old(title, "")
        insert_buf.append((
            str(uuid.uuid4()), grow["project_id"], grow["scheme_id"],
            group_id, group_title, title, content, category,
            (old.get("source_ref") if old else "") or "", 0, 1.0, 1, 0, "",
            (old.get("fact_key") if old else "") or normalize_key(title),
            *_carry_provenance(old), *_carry_dimensions(old, title, "")))
    # ✅ 事务守卫：DELETE 与 INSERT+commit 原子完成，异常回滚，避免悬空事务
    # （连接归还写池后 DELETE 被下个请求的 commit 连带提交 → 整组静默删除）。
    try:
        await db.execute(
            "DELETE FROM global_facts WHERE group_id=? AND project_id=? AND scheme_id=?",
            (group_id, grow["project_id"], grow["scheme_id"] or ""))
        for _row_vals in insert_buf:
            if len(_row_vals) != len(_GROUP_REBUILD_COLS):
                # 列名与取值必须逐一对应，否则 SQLite 报「N values for M columns」
                raise ValueError(
                    f"分组重建取值 {len(_row_vals)} 个 vs 列 "
                    f"{len(_GROUP_REBUILD_COLS)} 个，请同步 _carry_* 返回长度")
        await db.executemany(_GROUP_REBUILD_SQL, insert_buf)
        await db.commit()

    except Exception:
        await db.rollback()
        raise

    if grow["scheme_id"]:
        await invalidate_export_cache(db, grow["scheme_id"], facts_touched=True)
    else:
        await _invalidate_fact_scope_cache(db, "", str(grow.get("project_id") or ""))
    return {"ok": True}


@router.patch("/{fact_id}/resolve")
async def resolve_fact(
    fact_id: str,
    scheme_id: str = Query(""),
    db=Depends(get_db),
):
    """标记事实为已审核（模拟值闸门：未确认的模拟值禁止注入正文）"""
    scheme_scope = scheme_id.strip() if isinstance(scheme_id, str) else ""
    if scheme_scope:
        await _assert_fact_in_scheme_scope(db, fact_id, scheme_scope)
    cur = await db.execute(
        "SELECT scheme_id, is_simulated, has_conflict, is_stale FROM global_facts WHERE id=?",
        (fact_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "事实不存在")
    # 模拟值必须先改成有真实依据的取值；未裁决冲突必须先走 resolve-conflict。
    # 409 保留原状态，前端据此引导用户完成对应操作，避免绕过安全闸门。
    if bool(row["is_simulated"]):
        logger.warning("global_facts 409 闸门：fact_id=%s 是模拟值", fact_id)
        raise HTTPException(409, "模拟值不能直接确认，请先核对并修改为真实值")
    if bool(row["has_conflict"]):
        logger.warning("global_facts 409 闸门：fact_id=%s 存在多来源矛盾", fact_id)
        raise HTTPException(409, "事实存在多来源矛盾，请先选择正确的候选值")
    if bool(row["is_stale"]):
        logger.warning("global_facts 409 闸门：fact_id=%s 来源已变化", fact_id)
        raise HTTPException(409, "事实来源资料已变化，请重新提取或先编辑核对")

    await db.execute(
        "UPDATE global_facts SET is_resolved=1, is_stale=0, "
        "updated_at=datetime('now','localtime') WHERE id=?", (fact_id,))
    await db.commit()

    if row["scheme_id"]:
        await invalidate_export_cache(db, row["scheme_id"], facts_touched=True)
    else:
        cur = await db.execute("SELECT project_id FROM global_facts WHERE id=?", (fact_id,))
        prow = await cur.fetchone()
        await _invalidate_fact_scope_cache(db, "", str(prow[0] or "") if prow else "")
    return {"ok": True}


# ✅ 写侧不变量（G3 · 2026-10-04）：解除过期标记时**不得**破坏既有安全闸门。
#    保持不变式 `is_simulated=1 ⟹ is_resolved=0` 与「矛盾未裁决 ⟹ 不可确认」，
#    否则「确认资料仍有效」会被当成绕过「模拟值/矛盾值」审核闸门的捷径。
_ACK_STALE_RESOLVE_CASE = (
    "is_resolved=CASE WHEN COALESCE(is_simulated,0)=0 AND COALESCE(has_conflict,0)=0"
    " THEN 1 ELSE is_resolved END")


@router.patch("/{fact_id}/ack-stale")
async def ack_fact_stale(
    fact_id: str,
    scheme_id: str = Query(""),
    db=Depends(get_db),
):
    """人工核对确认「该取值仍然有效」，解除本条的 ``is_stale`` 过期标记。

    ✅ 缺口修复（G3 · 2026-10-04）：``is_stale=1`` 此前**没有解除入口**，构成死锁：

      · 打标记的路径是**批量**的 —— ``_mark_project_facts_stale``（本文件 :2777）
        在项目资料重新解析/删除时把**整个项目**的事实一次性置 ``is_stale=1``；
      · 解除标记的路径却只有两条，且都要求「值必须先变」：
        PATCH 事实（``value_changed`` 为真才 ``is_stale=0``，见 :1185）与
        ``resolve-conflict`` 裁决取值；
      · 而 ``resolve``（:1550）对 stale 行直接 409「请重新提取或先编辑核对」，
        ``batch-resolve``（:1801）同样把 stale 行整批跳过。

    于是：用户明明核对过、取值并未变化（资料重传/补传是常见操作，绝大多数事实
    并不会因此失效），却被永久排除在正文注入与导出门控之外（``_FACTS_INJECT_WHERE``
    含 ``is_stale=0``），而「来源过期 N」的红色 Tag 又无法消除 —— 只能逐条把值
    改成另一个值再改回来，或对数十上百条事实重新跑一次 AI 提取（真实计费）。

    语义：本端点只表达「我已核对，这个值仍然有效」，因此**只清 is_stale**；
    是否顺带置 ``is_resolved=1`` 由既有闸门决定（模拟值/矛盾值保持不确认，
    不变量见 :data:`_ACK_STALE_RESOLVE_CASE`），并在 ``blocked_reason`` 回传原因
    供前端继续引导。

    幂等：本条未处于过期状态时返回 ``changed=False``（不报错），前端可安全重复调用。
    """
    scheme_scope = scheme_id.strip() if isinstance(scheme_id, str) else ""
    if scheme_scope:
        await _assert_fact_in_scheme_scope(db, fact_id, scheme_scope)
    cur = await db.execute(
        "SELECT scheme_id, project_id, is_simulated, has_conflict, is_stale, is_resolved"
        " FROM global_facts WHERE id=?", (fact_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "事实不存在")

    if not bool(row["is_stale"]):
        return {"ok": True, "changed": False,
                "is_resolved": bool(row["is_resolved"]), "blocked_reason": ""}

    # ✅ G-04 修复（2026-10-04）：SELECT 判门槛 → UPDATE → commit 的三步路径
    #    需要显式事务守卫。UPDATE 已下推但 commit 前抛异常（DB 锁、连接中断）
    #    时，行仍处于悬空事务里；连接归还池后会被下一个请求的 commit 连带
    #    提交，形成"响应 500 但 is_stale 已被静默清除"的脏状态。与 create_fact
    #    / update_fact / clear_all_facts 同口径显式回滚。
    try:
        is_sim = bool(row["is_simulated"])
        has_conflict = bool(row["has_conflict"])
        if is_sim or has_conflict:
            await db.execute(
                "UPDATE global_facts SET is_stale=0, "
                "updated_at=datetime('now','localtime') WHERE id=?", (fact_id,))
            blocked = ("模拟值仍需先核对并改为真实取值，本次仅解除过期标记"
                       if is_sim else "多来源矛盾仍需先裁决取值，本次仅解除过期标记")
            new_resolved = bool(row["is_resolved"])
        else:
            await db.execute(
                "UPDATE global_facts SET is_stale=0, is_resolved=1, "
                "updated_at=datetime('now','localtime') WHERE id=?", (fact_id,))
            blocked = ""
            new_resolved = True
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    if row["scheme_id"]:
        await invalidate_export_cache(db, row["scheme_id"], facts_touched=True)
    else:
        await _invalidate_fact_scope_cache(db, "", str(row["project_id"] or ""))
    return {"ok": True, "changed": True,
            "is_resolved": new_resolved, "blocked_reason": blocked}


@router.post("/ack-stale")
async def batch_ack_stale(data: dict, db=Depends(get_db)):
    """批量解除过期标记（"这些值我都核对过，仍然有效"）。

    ✅ 为什么必须批量：`_mark_project_facts_stale` 是**项目级**批量打标的，
    一次资料重传就可能让数十上百条事实同时过期 —— 只有单条入口等于没有入口。

    ✅ 作用域与安全（与 ``batch-resolve`` 同口径）：
      · 必须传 ``scheme_id`` 限定作用域，防止 ``WHERE id IN (...)`` 越域改他方案数据；
      · 单条的安全闸门由 SQL 的 CASE 表达式保证（见 :data:`_ACK_STALE_RESOLVE_CASE`）：
        模拟值/矛盾值**只清 is_stale，不被顺带确认为已审核**，并在 ``gated`` 里
        回传条数，前端据此提示"N 条仍需逐条处理"。
    """
    if not isinstance(data, dict):
        raise HTTPException(400, "请求体必须是对象")
    fact_ids = data.get("fact_ids") or []
    scheme_id = str(data.get("scheme_id", "") or "").strip()
    if not isinstance(fact_ids, list):
        raise HTTPException(400, "fact_ids 必须是数组")
    if len(fact_ids) > 500:
        raise HTTPException(400, "单次最多处理 500 条事实")
    if not scheme_id:
        raise HTTPException(400, "需要提供 scheme_id 以限定作用域")
    cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
    srow = await cur.fetchone()
    if not srow:
        raise HTTPException(404, "方案不存在")
    real_pid = str(srow[0] or "")

    fact_ids = [str(f).strip() for f in fact_ids if str(f).strip()]
    scope_sql = (" AND (scheme_id=? OR (project_id=? AND "
                 "(scheme_id='' OR scheme_id IS NULL)))")
    scope_params: list = [scheme_id, real_pid]
    id_sql = ""
    if fact_ids:
        id_sql = f" AND id IN ({','.join('?' * len(fact_ids))})"
        scope_params = [*fact_ids, *scope_params]

    # ⚠️ 占位符顺序必须与 params 顺序逐位对应：`id IN (...)` 写在 scope 条件
    #    **之前**，与下面的 [*fact_ids, scheme_id, real_pid] 一致。
    #    （首版把 id_sql 拼在 scope_sql 之后、参数却把 fact_ids 放前面 →
    #     SQL 把 scheme_id 的值当 fact_id 比对，UPDATE 恒 0 行、零报错，
    #     属"静默不生效"——正是 safe_rowcount 也救不了的绑定错位。）
    # ✅ G-04 修复（2026-10-04）：COUNT + UPDATE + commit 是多语句写路径。
    #    UPDATE 已下推但 commit 前抛异常（DB 锁、连接中断、safe_rowcount 内
    #    rowcount 读取异常），几十到几百行仍留在悬空事务里；连接归还池后会被
    #    下一个请求的 commit 连带提交，形成"响应 500 但整批过期标记已解除"的
    #    脏状态。与单条 ack-stale / create_fact / update_fact 同口径显式回滚。
    try:
        gated = 0
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM global_facts WHERE is_stale=1"
            + id_sql + scope_sql +
            " AND (COALESCE(is_simulated,0)=1 OR COALESCE(has_conflict,0)=1)",
            scope_params)
        grow = await cur.fetchone()
        if grow:
            gated = int(grow[0] or 0)

        cur = await db.execute(
            "UPDATE global_facts SET is_stale=0, " + _ACK_STALE_RESOLVE_CASE +
            ", updated_at=datetime('now','localtime')"
            " WHERE is_stale=1" + id_sql + scope_sql, scope_params)
        changed = safe_rowcount(cur, what="批量解除事实过期标记")
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 事实集合变化 → 失效项目下所有方案缓存（可能改动了项目共享事实）
    await _invalidate_fact_scope_cache(db, "", real_pid)
    return {"ok": True, "changed": changed, "gated": gated}


@router.patch("/{fact_id}/resolve-conflict")
async def resolve_conflict(
    fact_id: str,
    data: dict,
    scheme_id: str = Query(""),
    db=Depends(get_db),
):
    """选择一个候选值解决矛盾：更新取值、清除矛盾标记、置为已确认。

    ✅ 增强：此前矛盾事实只能整体「确认」（沿用首个提取值），无法在多个
    候选值之间裁决。新增按值选择——前端在候选值列表点「选此值」即落库。

    ✅ BUG 修复（2026-09-21）：模拟值闸门（is_simulated）随裁决结果重算。
    旧实现只写 content / has_conflict / is_resolved，`is_simulated` 原样保留 →
    一条编造值被裁决为文档实值后，行上仍带模拟值标记，`stats.simulated` 永不
    归零；前端「全部就绪 / 下一步：正文生成」入口因此永远不出现，用户被卡在
    事实 Tab（矛盾已全部裁决完却仍显示"待确认模拟值 N 项"）。
    口径：
      - 值真正变化 → 按被采纳候选自带的 is_simulated 重算（候选即模拟值时
        闸门继续生效）；候选不在登记列表 / 旧数据无该字段 → 视为人工确认的
        真实取值，is_simulated 归 0；
      - 值未变（前端「保留当前值」）→ 只清矛盾标记，模拟值语义保持原样。
    """
    new_value = strip_simulated_marker(str(data.get("value") or "").strip())
    if not new_value:
        raise HTTPException(400, "需提供选择的值")
    scheme_scope = scheme_id.strip() if isinstance(scheme_id, str) else ""
    if scheme_scope:
        await _assert_fact_in_scheme_scope(db, fact_id, scheme_scope)
    cur = await db.execute(
        "SELECT id, title, content, is_simulated, conflict_keys, scheme_id "
        "FROM global_facts WHERE id=?", (fact_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "事实不存在")

    name = (row["title"] or "").strip() or "未命名"
    is_sim = bool(row["is_simulated"])
    prior_value = extract_value_from_markdown_line(row["content"] or "")[1]
    if prior_value.strip() != new_value:
        chosen = _find_conflict_candidate(row["conflict_keys"] or "", new_value)
        is_sim = bool(chosen.get("is_simulated")) if chosen else False
    # ✅ 统一 content 构造（与分组重建一致），避免两种模拟值标记写法
    #    在列表接口回解时出现解析差异。
    content = _build_fact_content(name, new_value, is_sim)
    # ✅ BUG 修复：人工裁决应把 confidence 拉满（1.0）并重置 has_conflict。
    # 旧实现只更新 content/has_conflict/is_resolved，confidence 保持原值，
    # 前端仍显示"低置信度"标签，与"已人工确认"的语义冲突。
    # 人工选择模拟候选只完成「冲突裁决」，不等于确认了真实值；保持未审核闸门。
    resolved_flag = 0 if is_sim else 1
    await db.execute(
        "UPDATE global_facts SET content=?, is_simulated=?, has_conflict=0, "
        "conflict_keys='', confidence=1.0, is_resolved=?, is_stale=0, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (content, 1 if is_sim else 0, resolved_flag, fact_id))
    await db.commit()

    if row["scheme_id"]:
        await invalidate_export_cache(db, row["scheme_id"], facts_touched=True)
    else:
        cur = await db.execute("SELECT project_id FROM global_facts WHERE id=?", (fact_id,))
        prow = await cur.fetchone()
        await _invalidate_fact_scope_cache(db, "", str(prow[0] or "") if prow else "")
    return {"ok": True}


@router.post("/clear")
async def clear_all_facts(data: dict, db=Depends(get_db)):
    """一键清除某方案（项目）下全部已提取的项目信息。

    ✅ 新增（2026-09-17）：此前没有任何「清空重来」入口 —— factsApi.delete
    定义了但前端无调用点，用户只能逐组删除。现提供作用域级清空：
    - 删除该 scheme 作用域下【全部】事实（含已确认/手动，操作前前端二次确认）；
    - 同步清空该项目的增量提取进度（facts_extracted_chunks），
      下次提取将全量重跑；
    - 联动清空 export_cache（含磁盘产物）。
    """
    scheme_id = str(data.get("scheme_id", "") or "").strip()
    if not scheme_id:
        raise HTTPException(400, "需要 scheme_id")
    cur = await db.execute(
        "SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    project_id = str(row[0] or "")

    try:
        cur = await db.execute(
            "SELECT count(*) FROM global_facts WHERE scheme_id=? OR "
            "(project_id=? AND (scheme_id='' OR scheme_id IS NULL))",
            (scheme_id, project_id))
        total = int((await cur.fetchone())[0] or 0)
        await db.execute(
            "DELETE FROM global_facts WHERE scheme_id=? OR "
            "(project_id=? AND (scheme_id='' OR scheme_id IS NULL))",
            (scheme_id, project_id))
        # 增量提取进度按作用域清空（下次提取全量重跑）。
        # ✅ 修复（2026-09-26）：旧实现仅在 project_id 非空时、且严格按
        #    (project_id, scheme_id) 删除，导致两类残留：
        #      (a) 未绑定 project 的方案（project_id=''）清空后增量指纹不清除，
        #          下次提取会跳过已哈希段落、新资料永不进入；
        #      (b) 项目共享级（scheme_id=''）指纹未被清理，与上方 facts 删除口径
        #          （含项目共享事实）不一致，残留指纹会让"重新提取"误判已覆盖。
        #    现按「方案级 + 项目共享级」双口径清理，与 facts 删除口径完全对齐。
        await db.execute(
            "DELETE FROM facts_extracted_chunks WHERE scheme_id=?", (scheme_id,))
        if project_id:
            await db.execute(
                "DELETE FROM facts_extracted_chunks WHERE project_id=? AND "
                "(scheme_id='' OR scheme_id IS NULL)", (project_id,))
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await _invalidate_fact_scope_cache(db, scheme_id, project_id)
    logger.info("已清除方案 %s 可见范围的全局事实（%d 条）并重置提取进度", scheme_id, total)
    return {"ok": True, "deleted": total}


@router.post("/batch-resolve")
async def batch_resolve(data: dict, db=Depends(get_db)):
    """批量确认事实（mark all as resolved）

    ✅ 安全约束（F1 遗留缺口修复）：批量确认时跳过以下条目，
    防止编造值/关键参数被一次性放行进入正文生成与导出链路：
    - is_simulated = 1：模拟值/编造值事实
    - is_safety_critical = 1：安全关键类事实（17 类白名单）
    被跳过的条目在响应体 `skipped_safety` 中显式返回，供前端提示。
    保持不变式：`is_simulated=1 ⟹ is_resolved=0`（模拟值闸门）。
    """
    fact_ids = data.get("fact_ids", [])
    scheme_id = str(data.get("scheme_id", "") or "").strip()
    if not isinstance(fact_ids, list):
        raise HTTPException(400, "fact_ids 必须是数组")
    if len(fact_ids) > 500:
        raise HTTPException(400, "单次最多确认 500 条事实")
    # ✅ 安全加固（2026-09-26）：指定条目批量确认时必须提供 scheme_id 以限定
    #    作用域，否则 WHERE id IN (...) 无作用域约束 → 可越域确认其他方案的事实。
    #    前端「批量确认选中」固定携带 schemeId，故向后兼容；仅拦截缺失作用域的调用。
    if fact_ids and not scheme_id:
        raise HTTPException(400, "批量确认指定条目需要提供 scheme_id 以限定作用域")

    skipped_safety: list[dict] = []
    changed_ids: list[str] = []
    skipped_count = 0  # 已确认/不在范围内，无需变更

    if fact_ids:
        fact_ids = [str(fid).strip() for fid in fact_ids if str(fid).strip()]
        if not fact_ids:
            return {"ok": True, "updated": 0,
                    "skipped_safety": [], "skipped_safety_count": 0,
                    "safety_blocked": False}
        placeholders = ",".join("?" * len(fact_ids))
        if scheme_id:
            cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
            scheme_row = await cur.fetchone()
            if not scheme_row:
                raise HTTPException(404, "方案不存在")
            real_pid = str(scheme_row[0] or "")
            scope = (
                " AND (scheme_id=? OR (project_id=? AND "
                "(scheme_id='' OR scheme_id IS NULL)))"
            )
            params = [*fact_ids, scheme_id, real_pid]
        else:
            scope, params = "", fact_ids

        # 先查询目标事实，检测安全约束
        cur = await db.execute(
            f"SELECT id, title, fact_key, is_simulated, is_resolved, has_conflict, is_stale "
            f"FROM global_facts WHERE id IN ({placeholders}){scope}",
            params)
        rows = [dict(r) for r in await cur.fetchall()]

    elif scheme_id:
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
        scheme_row = await cur.fetchone()
        if not scheme_row:
            raise HTTPException(404, "方案不存在")
        real_pid = str(scheme_row[0] or "")
        cur = await db.execute(
            "SELECT id, title, fact_key, is_simulated, is_resolved, has_conflict, is_stale "
            "FROM global_facts WHERE scheme_id=? OR (project_id=? AND "
            "(scheme_id='' OR scheme_id IS NULL))",
            (scheme_id, real_pid))
        rows = [dict(r) for r in await cur.fetchall()]
    else:
        raise HTTPException(400, "需要 scheme_id 才能批量确认")

    # 安全关键判定：以名称/类别白名单启发式为准，辅以落库的 is_safety_critical 列。
    # ✅ 数据流审计 2026-09-23：该列已幂等补入所有运行库，`has_safety_col` 恒为 True；
    #    但历史行/手动行未经 to_db_row 写入时列默认 0，若仅信列会漏拦。
    #    故改为「启发式 或 列标记」：启发式始终作为权威下限，列作为补充信号。
    has_safety_col = False
    try:
        cur = await db.execute("PRAGMA table_info(global_facts)")
        cols = {dict(r)["name"] for r in await cur.fetchall()}
        has_safety_col = "is_safety_critical" in cols
    except Exception as e:
        # ✅ A-4（2026-10-01）：原为 `except Exception: pass`。该处探测
        #    is_safety_critical 列是否存在，失败时 has_safety_col=False 会
        #    **弱化**安全门槛（列存在但探测失败 → 漏拦危大事实），必须留痕。
        #    行为保持不变（仍按 False 走启发式下限），仅补可观测性。
        logger.warning(
            "探测 global_facts.is_safety_critical 列失败（按不存在处理，"
            "安全门槛退化为启发式下限）: %s", e, exc_info=True)

    for row in rows:
        # 已确认的跳过（幂等）
        if row["is_resolved"]:
            skipped_count += 1
            continue

        # 安全约束检查
        is_sim = bool(row.get("is_simulated"))
        is_stale = bool(row.get("is_stale"))
        has_conflict = bool(row.get("has_conflict"))
        is_safety = is_safety_critical_name(
            row.get("title", ""), "", row.get("fact_key", "")) or (
            bool(row.get("is_safety_critical")) if has_safety_col else False)

        if is_sim:
            skipped_safety.append({
                "id": row["id"],
                "title": row.get("title", ""),
                "reason": "模拟值事实禁止批量确认，请逐条裁决并补充真实依据",
            })
        elif has_conflict:
            skipped_safety.append({
                "id": row["id"], "title": row.get("title", ""),
                "reason": "多来源矛盾尚未裁决，禁止批量确认",
            })
        elif is_stale:
            skipped_safety.append({
                "id": row["id"], "title": row.get("title", ""),
                "reason": "来源资料已变化，请重新提取或人工核对",
            })
        elif is_safety:
            skipped_safety.append({
                "id": row["id"],
                "title": row.get("title", ""),
                "reason": "安全关键事实禁止批量确认，请逐条确认",
            })
        else:
            changed_ids.append(row["id"])

    # ✅ G-04 修复（2026-10-04）：先 SELECT 判门槛、再 UPDATE 落库、再 commit 是
    #    **多语句**写路径。UPDATE 后 commit 前若抛异常（连接中断、DB 锁），
    #    已更新行仍在悬空事务里；连接归还池后会被下一个请求的 commit 连带提交，
    #    让「批量确认」在返回 500 后仍部分生效。与 create_fact / update_fact
    #    同口径显式回滚。
    try:
        # 执行更新（仅安全条目的）
        if changed_ids:
            ph = ",".join("?" * len(changed_ids))
            await db.execute(
                f"UPDATE global_facts SET is_resolved=1, "
                f"updated_at=datetime('now','localtime') "
                f"WHERE id IN ({ph})",
                changed_ids)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    if scheme_id:
        # 批量确认可能同时改变项目共享事实，失效项目下所有方案缓存。
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
        prow = await cur.fetchone()
        if prow:
            await _invalidate_fact_scope_cache(db, "", str(prow[0] or ""))
        else:
            await invalidate_export_cache(db, scheme_id, facts_touched=True)

    return {
        "ok": True,
        "changed": len(changed_ids),
        "skipped": skipped_count,
        "skipped_safety": skipped_safety,
        "skipped_safety_count": len(skipped_safety),
        "safety_blocked": len(skipped_safety) > 0,
    }


# ---------------------------------------------------------------------------
# 全局事实 AI 自然语言调整（引入自参考软件 globalFactsAdjustmentTask，2026-09-22）
# ---------------------------------------------------------------------------

# 合法分类集合：以 CATEGORY_TITLES 的键（= fact_type 枚举）为准，兼容 other。
_ALLOWED_FACT_CATEGORIES = set(CATEGORY_TITLES.keys()) | {"other"}


def _facts_for_adjust_prompt(rows: list) -> list:
    """把事实行转为喂给 AI 的精简结构：fact_id / name / value / category。

    ✅ 不携 source/conflict 等运营字段（与目录调整同思路：降 token、
    避免模型误改无关字段）；fact_id 作为稳定引用键供 AI 回传。
    """
    out = []
    for r in rows:
        nm, val = extract_value_from_markdown_line(r.get("content") or "")
        out.append({
            "fact_id": r["id"],
            "name": nm or (r.get("title") or ""),
            "value": val or (r.get("content") or ""),
            "category": r.get("category") or "other",
        })
    return out


def _validate_adjust_ops(obj, valid_ids: set) -> tuple:
    """校验 AI 操作计划，返回 (合法操作列表, summary)。非法项一律丢弃（绝不将就）。

    核心防线：update/delete 的 fact_id 必须命中现有真实行（AI 幻觉 id → 丢弃，
    避免误删/误改别的方案）；add 必须有 name+value；category 非法→ other。
    """
    raw = obj.get("operations") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        raw = []
    clean = []
    for op in raw:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").strip().lower()
        if kind == "update":
            fid = str(op.get("fact_id") or "").strip()
            if fid not in valid_ids:
                continue
            upd = {"op": "update", "fact_id": fid}
            if op.get("value") is not None and str(op.get("value")).strip():
                upd["value"] = str(op.get("value"))
            if op.get("name") is not None and str(op.get("name")).strip():
                upd["name"] = str(op.get("name")).strip()
            if op.get("category"):
                cat = str(op["category"]).strip()
                upd["category"] = cat if cat in _ALLOWED_FACT_CATEGORIES else "other"
            if len(upd) > 2:  # 除 op+fact_id 外至少有一个实质变更
                clean.append(upd)
        elif kind == "delete":
            fid = str(op.get("fact_id") or "").strip()
            if fid in valid_ids:
                clean.append({"op": "delete", "fact_id": fid,
                              "reason": str(op.get("reason") or "")[:200]})
        elif kind == "add":
            nm = str(op.get("name") or "").strip()
            val = "" if op.get("value") is None else str(op.get("value")).strip()
            if not nm or not val:
                continue
            cat = str(op.get("category") or "").strip()
            clean.append({"op": "add", "name": nm, "value": val,
                          "category": cat if cat in _ALLOWED_FACT_CATEGORIES else "other"})
    return clean, str(obj.get("summary") or "").strip()


@router.post("/adjust")
async def adjust_facts(data: dict, db=Depends(get_db)):
    """全局事实 AI 自然语言调整：按用户要求产出最小操作计划（可选直接应用）。

    【引入背景】参考软件支持用自然语言批量修改已有事实（如"把涉及的年份统一改成 2026"、
    "新增一条：项目经理=张伟"）；本软件此前只能逐条 CRUD 或全量重提取。

    【与参考软件的取舍】参考软件持久会话直接重写 global-facts.json 文件；本软件事实
    库带溯源/矛盾/模拟值闸门等不变式，若让 AI 重写整库会冲掉这些元数据。故改为
    返回**按 fact_id 定位的最小操作计划**，应用时全走既有写路径（update →
    _apply_item_updates、delete → 行级删除、add → 单行插入），天然保住不变式。

    【兼容性】默认 apply=False，仅返回待确认计划，零数据风险；apply=True 时才落库。
    未引入任何图表/人工配图逻辑（严守全自动图表约束）。
    """
    instruction = str(data.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "需要提供 instruction（调整要求）")
    scheme_id = data.get("scheme_id") if isinstance(data.get("scheme_id"), str) else ""
    project_id = data.get("project_id") if isinstance(data.get("project_id"), str) else ""
    do_apply = bool(data.get("apply"))
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    scheme_scope = scheme_id.strip() if isinstance(scheme_id, str) else ""

    # 载入当前事实（有 scheme 限定本方案，否则整个项目）
    sql = "SELECT id, title, content, category FROM global_facts WHERE project_id=?"
    params = [real_pid]
    if scheme_scope:
        sql += " AND scheme_id=?"
        params.append(scheme_scope)
    cur = await db.execute(sql, params)
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        raise HTTPException(400, "当前作用域还没有任何全局事实，请先提取或新增")

    valid_ids = {r["id"] for r in rows}
    confirmed_ops = data.get("operations")
    if do_apply and isinstance(confirmed_ops, list):
        # 用户已在 UI 预览并二次确认具体操作；直接校验这份计划，禁止再次调用 AI
        # 生成另一份可能不同的计划（否则“确认内容”与“实际落库内容”存在漂移）。
        ops, summary = _validate_adjust_ops({"operations": confirmed_ops}, valid_ids)
    else:
        prompt = render(
            "global_facts_adjust_system",
            current_facts=json.dumps(_facts_for_adjust_prompt(rows), ensure_ascii=False)[:30000],
            instruction=instruction[:3000],
        )
        try:
            obj, _ = await asyncio.wait_for(
                collect_json_response(
                    [{"role": "system", "content": prompt}],
                    lambda o: [] if isinstance(o, dict) and "operations" in o
                    else ["缺少 operations 字段"],
                    json_mode=True, temperature=0.2, scene="global_facts_adjust"),
                timeout=settings.ai_adjust_timeout)
        except asyncio.TimeoutError:
            raise HTTPException(504, "事实调整超时，请缩小调整范围后重试")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("全局事实 AI 调整失败: %s", e)
            raise HTTPException(502, f"事实调整失败：{e}")
        ops, summary = _validate_adjust_ops(obj, valid_ids)

    applied = {"updated": 0, "added": 0, "deleted": 0}
    if do_apply and ops:
        # ✅ G-04 修复（2026-10-04）：AI 调整可能下发多条 update/delete/add，
        #    循环中**每条都会写库但不 commit**（末尾统一提交）。中间任何一条
        #    抛异常（例如 op["fact_id"] 在并发删除下消失、SQLite 锁），前 N-1
        #    条已经下推的写仍留在悬空事务里；连接归还池后会被下一个请求的
        #    commit 连带提交，形成"部分调整生效但前端拿到 500"的脏状态。
        #    与 create_fact / update_fact / clear_all_facts 同口径显式回滚。
        try:
            for op in ops:
                if op["op"] == "update":
                    n, _sid = await _apply_item_updates(db, [{
                        "fact_id": op["fact_id"],
                        "value": op.get("value"),
                        "name": op.get("name"),
                        "category": op.get("category"),
                    }])
                    applied["updated"] += n
                elif op["op"] == "delete":
                    await db.execute("DELETE FROM global_facts WHERE id=?", (op["fact_id"],))
                    applied["deleted"] += 1
                else:  # add
                    content = _build_fact_content(op["name"], op["value"], False)
                    # ✅ BUG 修复（2026-10-01）：列清单改走 MANUAL_FACT_INSERT_SQL 单一
                    #    出口（原为手写 15 列，与 create_fact 的两条分支各自维护）。
                    await db.execute(
                        MANUAL_FACT_INSERT_SQL,
                        _manual_fact_row(
                            fid=str(uuid.uuid4()), pid=real_pid, sid=scheme_scope,
                            group_id=str(uuid.uuid4()),
                            group_title=CATEGORY_TITLES.get(op["category"], ""),
                            name=op["name"], content=content,
                            category=op["category"],
                            # 溯源标记为 AI 调整：_is_protected 据此区分人工与 AI 来源，
                            # 避免 AI 新增的事实在下次提取时被当人工录入保护下来。
                            source_file="AI调整",
                            is_simulated=False,
                            confidence=1.0,
                            is_resolved=True,
                        ))
                    applied["added"] += 1
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        # ✅ 2026-09-29：项目级（scheme_scope 为空）调整此前不失效任何缓存，
        #    与 create_fact / update_fact / delete_fact 口径对齐。
        await _invalidate_fact_scope_cache(db, scheme_scope, str(real_pid))
    return {
        "ok": True,
        "operations": ops,
        "summary": summary,
        "applied": applied if do_apply else None,
    }


@router.delete("/{fact_id}")
async def delete_fact(
    fact_id: str,
    scheme_id: str = Query(""),
    db=Depends(get_db),
):
    # ✅ 修复 P1：先查后删（原实现先 DELETE 再 SELECT，缓存失效永远不生效）
    # ✅ 修复 P2：fact_id 兼容行 id 与分组 group_id —— 前端「删除分组」传
    #    group.id，分组经编辑重建后行 id 均为新 uuid，原实现只按 id 匹配
    #    会删除 0 行且返回 ok，导致分组永远删不掉。
    scheme_scope = scheme_id.strip() if isinstance(scheme_id, str) else ""
    if scheme_scope:
        await _assert_fact_in_scheme_scope(db, fact_id, scheme_scope)
    cur = await db.execute(
        "SELECT id, project_id, scheme_id, group_id FROM global_facts WHERE id=? OR group_id=? LIMIT 1",
        (fact_id, fact_id))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "事实不存在")

    # group_id 不是全局唯一约束（历史数据/复制方案可能复用），删除分组时
    # 必须把作用域限制在命中的方案，避免误删其它方案的同组事实。
    if row["id"] == fact_id:
        await db.execute("DELETE FROM global_facts WHERE id=?", (fact_id,))
    else:
        await db.execute(
            "DELETE FROM global_facts WHERE group_id=? AND project_id=? AND scheme_id=?",
            (fact_id, row["project_id"], row["scheme_id"] or ""))
    await db.commit()

    if row["scheme_id"]:
        await invalidate_export_cache(db, row["scheme_id"], facts_touched=True)
    else:
        await _invalidate_fact_scope_cache(db, "", str(row["project_id"] or ""))
    return {"ok": True}


# ---------------------------------------------------------------------------
# 资料文档管理
# ---------------------------------------------------------------------------

@router.get("/documents")
async def list_documents(
    project_id: str = "",
    scheme_id: str = "",
    db=Depends(get_db),
):
    """列出项目资料文档。

    ✅ 加固：支持直接传 scheme_id（内部反查 project_id）。
    此前前端完全依赖 sections 接口返回的 scheme.project_id：一旦该字段缺失
    （如后端未重启、旧版本返回），查询参数为空 → 前端静默跳过 → 列表恒为空，
    表现为「上传了文件却不显示、像是没保存」，而文件其实已正常落库落盘。

    ✅ BUG 修复（跨项目数据泄漏）：旧实现无作用域时返回【全部项目】的文档列表。
    与 list_facts 口径对齐：缺 scheme_id/project_id 直接 400。

    ⚠️ 排序差异（有意设计，勿"统一"）：
    - 本函数 `ORDER BY created_at DESC`：最新上传在前，符合【列表展示】直觉；
    - `load_parsed_docs` `ORDER BY created_at, id`：按上传先后，供【下游消费】
      （事实提取按资料顺序拼 `=== 文件名 ===` 来源标注、批量解析按上传顺序
      给进度）。两者口径不同是刻意保留的，改任何一侧前先核对全部调用方。
    """
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    sql = ("SELECT id, project_id, file_name, file_type, doc_type, "
           "length(parsed_markdown) as text_len, "
           "doc_category, file_size, parse_time, parse_warnings, "
           "parse_truncated, "
           "parse_status, created_at FROM project_documents")
    params: list = [real_pid]
    sql += " WHERE project_id=?"
    sql += " ORDER BY created_at DESC"
    cur = await db.execute(sql, params)
    # ✅ P1（R13 漏改 · 2026-10-05）：读路径判空。此前直接 `await cur.fetchall()`
    #    → AttributeError → 500。**不得**降级成空列表 —— 前端会把「数据库瞬时
    #    故障」显示成「项目下没有资料」，用户据此重新上传或去查为什么文件丢了，
    #    而文件其实完好在库。503 语义 = 可重试的暂不可用（同 _load_doc 口径）。
    if cur is None:
        logger.warning("查询文档列表失败（db.execute 返回 None），project=%s", real_pid)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    rows = [dict(r) for r in await cur.fetchall()]
    # ✅ 增强：解析器诊断告警（JSON 列 → 数组回传，畸形数据容错为空数组）
    for r in rows:
        r["parse_warnings"] = _decode_parse_warnings(r.get("parse_warnings"))
        # ✅ F2（2026-09-26）：优先读取持久化的解析器级截断标记；旧库（该列为空）
        #    回退到按字数反推，覆盖两种口径，避免漏报「PDF 截页/表格截行」类截断。
        r["truncated"] = bool(r.get("parse_truncated")) or _is_truncated(r.get("text_len"))
    return {"documents": rows}


async def load_parsed_docs(db, project_id: str,
                           limit: int | None = None,
                           truncated_out: list | None = None) -> list[tuple[str, str]]:
    """✅ 唯一读取入口：按上传先后取该项目【已解析】文档的 (file_name, 正文)。

    - 仅返回正文非空的文档；`limit=None` 表示不限份数（全量，供事实提取使用）。
    - 「非空」条件下推到 SQL、并按 (created_at, id) 固定排序 —— 旧写法
      （先 LIMIT 再在 Python 里过滤空值）会让新上传的未解析文档占满名额，
      已解析资料一份都取不到，生成端静默退化成"只有工程类型 + 项目名称"。
    - 需要**文件名**的调用方（如事实提取要拼 `=== 文件名 ===` 做来源标注）用本
      函数；只需要正文的用 load_parsed_texts（它委托到本函数）。

    ✅ 2026-09-30（第十四轮 · P0 静默截断）：本函数此前只取
    ``(file_name, parsed_markdown)``、**不读** ``parse_truncated`` ——
    而解析阶段已把「PDF 截页 / 表格截行 / 落库字数超限」写进该列。于是目录
    生成（limit=5）与正文生成（limit=3）都可能在**残缺文本**上工作，而用户
    只看到「生成成功」。（事实提取链路已在 §4.18.2 单独收口。）
    现按实际列集合动态拼装（缺列只降级该列），并：
      · 恒记一条 WARNING（即使调用方不传 ``truncated_out``，也能在日志里
        看到「本次生成用了截断文档」）——**可观测性不应依赖调用方记得传参**；
      · 调用方传入 ``truncated_out`` 列表时，把被截断的文件名写进去自行处置。

    截断**不阻止**使用：残缺依据仍比没有依据好，静默丢文档会让生成直接失败。
    """
    if not project_id:
        return []
    base = ("SELECT file_name, parsed_markdown FROM project_documents "
            "WHERE project_id=? AND parsed_markdown IS NOT NULL AND parsed_markdown!='' "
            "ORDER BY created_at, id")
    tail = ""
    params: list = [project_id]
    if limit is not None:
        try:
            n = max(int(limit), 1)
        except (TypeError, ValueError):
            n = 5
        tail = " LIMIT ?"
        params.append(n)

    pairs, truncated = await _query_docs_with_diag(db, base, tail, tuple(params))
    if truncated:
        logger.warning(
            "生成链路使用了 %d 份被截断的文档（依据不完整）：%s；"
            "建议在「全局事实」页重新解析或拆分文件后重新上传",
            len(truncated), "、".join(truncated[:5]))
        if truncated_out is not None:
            truncated_out.extend(truncated)
    return pairs


async def _query_docs_with_diag(db, base: str, tail: str, params: tuple):
    """按可用列动态拼装 SQL 读取已解析文档，返回 ``(pairs, truncated_names)``。

    三层降级（任一失败都不阻断读路径）：
    ① 有 ``parse_truncated`` 列 → 直接读；
    ② 仅有 ``parse_warnings`` 列（旧库）→ 按告警文本含「截断」兜底；
    ③ 两列都没有 / 读失败 → 只取基础两列（= 引入前行为）。
    """
    pairs: list[tuple[str, str]] = []
    truncated: list[str] = []
    try:
        cols: set = set()
        try:
            cur = await db.execute("PRAGMA table_info(project_documents)")
            rows = await cur.fetchall()
            cols = {str(r[1]) for r in rows} if rows else set()
        except Exception:  # pragma: no cover
            cols = set()
        has_trunc = "parse_truncated" in cols
        has_warn = "parse_warnings" in cols
        if not (has_trunc or has_warn):
            cur = await db.execute(base + tail, params)
            for r in await cur.fetchall():
                if r[1]:
                    pairs.append((r[0] or "", r[1]))
            return pairs, truncated
        extra = ([("parse_truncated")] if has_trunc else []) + \
                ([("parse_warnings")] if has_warn else [])
        sql = ("SELECT file_name, parsed_markdown, " + ", ".join(extra)
               + " FROM project_documents "
               "WHERE project_id=? AND parsed_markdown IS NOT NULL AND parsed_markdown!='' "
               "ORDER BY created_at, id" + tail)
        cur = await db.execute(sql, params)
        for r in await cur.fetchall():
            d = dict(r)
            if not d.get("parsed_markdown"):
                continue
            pairs.append((d.get("file_name") or "", d["parsed_markdown"]))
            if has_trunc and d.get("parse_truncated"):
                truncated.append(d.get("file_name") or "")
            elif not has_trunc and "截断" in str(d.get("parse_warnings") or ""):
                truncated.append(d.get("file_name") or "")
    except Exception as e:  # noqa: BLE001 - 诊断列读取失败不应阻断生成
        logger.warning("读取文档截断诊断失败（按未截断处理）: %s", e)
        pairs = []
        # ⚠️ 兜底本身也必须包 try：DB 整体不可用时，「再查一次基础列」会抛第二
        #    个异常并**穿透**出去，把「降级为不判截断」变成「提取/生成直接崩」。
        try:
            cur = await db.execute(base + tail, params)
            for r in await cur.fetchall():
                if r[1]:
                    pairs.append((r[0] or "", r[1]))
        except Exception as e2:  # noqa: BLE001
            logger.warning("读取已解析文档失败（降级为空）: %s", e2)
    return pairs, truncated


async def load_parsed_texts(db, project_id: str, limit: int = 5,
                            truncated_out: list | None = None) -> list[str]:
    """（兼容入口）只返回已解析文档的正文，最多 limit 份。

    ✅ 统一入口：完整语义见 load_parsed_docs —— 本函数委托它，避免"已解析文档"
    的 SQL 在多处各写一份而漂移（历史上正是这种漂移导致过资料取不到）。

    ``truncated_out``（可选，2026-09-30 第十四轮新增）：被截断文档的文件名列表
    出参，供调用方把「依据不完整」显式告知用户；不传时 ``load_parsed_docs``
    仍会恒记 WARNING，可观测性不依赖调用方记得传参。
    """
    return [text for _name, text in
            await load_parsed_docs(db, project_id, limit, truncated_out)]


def _decode_parse_warnings(raw) -> list[str]:
    """把 parse_warnings 列（JSON 数组字符串）安全解码为字符串数组。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(w) for w in parsed if w]


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, db=Depends(get_db)):
    cur = await db.execute(
        "SELECT file_path, project_id FROM project_documents WHERE id=?", (doc_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    # 只删除事实上传目录内的文件。历史/手工写入的异常路径只清理数据库，
    # 避免删除接口被利用为任意文件删除器。
    fpath = row["file_path"] if "file_path" in row.keys() else ""
    candidate = _managed_upload_path(fpath)
    if candidate and candidate.exists():
        try:
            candidate.unlink()
        except OSError as e:
            logger.warning("删除原始文件失败（忽略）: %s", e)
    elif fpath:
        logger.warning("拒绝删除上传目录外的文档路径: %s", fpath)
    await db.execute("DELETE FROM project_documents WHERE id=?", (doc_id,))
    await db.commit()
    # ✅ 四层存储清理：解析层/提取层/语义层目录 + doc_chunks/doc_extractions/
    #    doc_validation_reports 关联行 + 文档索引条目（项目级事实不删，多文档共享）
    proj_id = row["project_id"] if "project_id" in row.keys() else ""
    if proj_id:
        try:
            await doc_pipeline.purge_document(db, doc_id=doc_id,
                                              project_id=proj_id)
        except Exception:
            logger.exception("文档 %s 四层产物清理失败（记录已删除）", doc_id)
        # ✅ 跨模块链路收敛：删除源文档后旧事实立即退出生成链路，并失效所有方案缓存。
        stale_count = await _mark_project_facts_stale(db, proj_id)
        await db.commit()
        await _invalidate_project_export_caches(db, proj_id)
        logger.info("删除文档 %s，标记项目 %s 的 %d 条事实为 stale",
                    doc_id, proj_id, stale_count)
    return {"ok": True, "stale_facts": stale_count if proj_id else 0}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ✅ 分步工作流（2026-09）：
#    ① 上传保存（仅存原始文件，不解析不提取）
#    ② 解析（用户点击 → parse_file_content → parsed_markdown 落库）
#    ③ 提取（用户点击 → SSE /sse/generate-facts 基于已解析文档提取）
#    ④ 修改（列表编辑/确认/删除，既有功能）
# ---------------------------------------------------------------------------

# ✅ 历史落库上限。早期版本按 80000 字截断，这些文档的长度恰好等于旧上限，
#    若只按当前 MAX_PARSED_CHARS 判定会被误判为"未截断" → 前端永远不显示
#    「重新解析」入口，旧截断文档无法自愈。
_LEGACY_PARSE_LIMITS: tuple[int, ...] = (80_000,)


def _is_truncated(text_len) -> bool:
    """判断文档文本是否疑似被截断（当前上限 或 历史上限 命中）。"""
    try:
        n = int(text_len or 0)
    except (TypeError, ValueError):
        return False
    if n <= 0:
        return False
    # 注意：本函数入参是**已落库文本的长度**，而落库时 `stored = text[:MAX]` ——
    # 只要发生过截断，落库长度就恰好等于 MAX。故此处必须用 `>=`（用 `>` 会漏判
    # 所有真实截断），与写入侧 `len(text) > MAX` 判定互为补充、并不矛盾。
    return n >= MAX_PARSED_CHARS or n in _LEGACY_PARSE_LIMITS


# Windows 保留设备名：不区分大小写，且「主名命中即保留」，带任意扩展名
# （CON.txt / NUL.doc）在 Windows 上仍指向设备，落盘会失败或写入错误位置。
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _safe_filename(raw: str) -> str:
    """清洗上传文件名：剥离路径（防目录穿越）、去除非法字符与控制字符、
    规避 Windows 保留设备名。

    ✅ BUG 修复：旧实现直接用客户端提供的 filename 参与路径拼接与落盘。
    部分客户端（旧版 IE/某些 HTTP 库）会携带完整路径（如 "C:\\a\\b.docx"），
    在 Windows 上会导致落盘路径被篡改；控制字符与通配符亦会造成写盘失败。
    ✅ 加固：CON/NUL/PRN/AUX/COM1-9/LPT1-9（含带扩展名形式）此前漏网，
    在 Windows 落盘时会命中设备名（如 NUL）导致文件静默丢失；统一加下划线前缀。
    """
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    if not name:
        return "uploaded_file"
    stem = name.split(".", 1)[0].strip().upper()
    if stem in _WINDOWS_RESERVED_STEMS:
        name = f"_{name}"
    return name


def _signature_valid(ftype: str, prefix: bytes) -> bool:
    """对常见二进制格式做轻量文件头校验，拒绝扩展名伪造。

    ✅ 增强：签名表已抽到 `file_parser.signature_valid` 统一维护 ——
    目录识别上传链路（upload-outline）此前完全没有这道校验，两条上传
    路径的防护口径不一致。此处保留原函数名以兼容既有调用/测试。
    """
    return signature_valid(ftype, prefix)


async def _stream_to_disk(f: UploadFile, dest: Path, limit: int) -> int:
    """流式落盘：分块读取，避免整份文件（可达 30MB×N）一次性驻留内存。

    返回写入字节数；超过 limit 时删除半成品并返回 -1。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with dest.open("wb") as out:
        while True:
            chunk = await f.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                break
            out.write(chunk)
    if total > limit:
        try:
            dest.unlink()
        except OSError:
            pass
        return -1
    return total


async def _resolve_project_id(db, scheme_id: str, project_id: str) -> str:
    """解析并校验资料作用域，禁止客户端伪造跨项目 scheme_id。"""
    # 只接受真正的字符串：直接以函数方式调用路由（测试 / 内部复用）时，
    # FastAPI 的 Query(...) 默认值会以【对象】形式传入，str() 化后得到
    # "Query('')" 这种假值 → 被误判为"scheme_id 与 project_id 不匹配"，
    # 报错信息完全指向错误方向。非字符串一律按"未提供"处理。
    sid = scheme_id.strip() if isinstance(scheme_id, str) else ""
    pid = project_id.strip() if isinstance(project_id, str) else ""
    if sid:
        cur = await db.execute(
            "SELECT project_id FROM schemes WHERE id=?", (sid,))
        row = await cur.fetchone()
        if not row or not row[0]:
            raise HTTPException(404, "方案不存在")
        real_pid = str(row[0])
        if pid and pid != real_pid:
            raise HTTPException(400, "scheme_id 与 project_id 不匹配")
        return real_pid
    if pid:
        cur = await db.execute("SELECT id FROM projects WHERE id=?", (pid,))
        if not await cur.fetchone():
            raise HTTPException(404, "项目不存在")
        return pid
    return ""


def _doc_file_path(project_id: str, doc_id: str, fname: str) -> Path:
    safe_name = _safe_filename(fname)
    return FACT_UPLOADS_DIR / project_id / f"{doc_id[:8]}_{safe_name}"


def _managed_upload_path(raw_path: str | Path) -> Path | None:
    """返回事实上传目录内的安全路径，目录外路径返回 None。"""
    if not raw_path:
        return None
    try:
        root = FACT_UPLOADS_DIR.resolve()
        candidate = Path(raw_path).resolve()
        candidate.relative_to(root)
        return candidate
    except (OSError, ValueError, RuntimeError):
        return None


@router.post("/upload-documents")
async def upload_documents(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    files: list[UploadFile] = File(...),
    db=Depends(get_db),
):
    """① 上传保存：仅保存原始文件与档案记录，不做解析与提取。

    同名文件重复上传时替换旧记录（含旧文件删除）。
    """
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    # ✅ 2026-09-23 日志埋点：上传入口生成全链路 trace_id，并绑定 project 上下文，
    #    使后续解析 / 提取 / 保存 / 分类的日志都能凭 trace_id 串联（见 log_context）。
    set_context(trace_id=new_trace_id(), project_id=real_pid, scheme_id=scheme_id)

    saved: list[dict] = []
    oversize: list[str] = []
    unsupported: list[str] = []
    signature_invalid: list[str] = []
    empty_files: list[str] = []
    replaced = 0
    written: list[Path] = []  # 已落盘文件，整体异常时回滚，避免产生孤儿文件
    old_paths_to_delete: list[Path] = []  # DB 提交成功后再清理，保证可回滚
    # ✅ BUG 修复（同名替换孤儿泄漏）：被替换旧文档只删了 project_documents 行，
    #    其 doc_chunks 行、提取/校验产物与四层磁盘目录无人清理 ——
    #    /documents/{id}/chunks 等按 doc_id 查询的入口虽在，索引却查不到该文档，
    #    旧块与旧层目录永久残留（占空间且与「删除文档即 purge_document」口径不一致）。
    #    与旧原件同策略：commit 成功后再清理，失败可回滚。
    purged_old_doc_ids: list[tuple[str, str]] = []  # (doc_id, project_id)

    # ✅ 配额：单请求文件数上限。超出部分直接拒绝，不做任何落盘。
    too_many: list[str] = []
    if len(files) > MAX_UPLOAD_FILES_PER_REQUEST:
        too_many = [(x.filename or "uploaded_file")
                    for x in files[MAX_UPLOAD_FILES_PER_REQUEST:]]
        files = files[:MAX_UPLOAD_FILES_PER_REQUEST]
    total_bytes = 0
    quota_exceeded = False
    # ✅ 回归（2026-09-21）：累计体积超限被拒的文件必须归入 quota_files 独立上报，
    #    不得混进 oversize —— oversize 语义是「单文件超 30MB」，前端据此提示
    #    「超过 30MB」；一个 8 字节文件因累计配额被拒时混入 oversize 会让用户
    #    误以为文件太大去拆分重试（错误归因）。
    quota_files: list[str] = []

    try:
        for _idx, f in enumerate(files):
            raw_name = f.filename or "uploaded_file"
            fname = _safe_filename(raw_name)
            ftype = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            # 格式预检：不支持的扩展名直接拒绝，避免存了却解析不了
            if ftype and ftype not in SUPPORTED_EXTENSIONS:
                unsupported.append(raw_name)
                continue
            # 大小预检：优先用 Content-Length（f.size），避免大文件白读一遍
            declared_size = getattr(f, "size", None)
            if declared_size and declared_size > MAX_UPLOAD_BYTES:
                oversize.append(raw_name)
                continue
            # 文件头校验只读取极小前缀并复位指针，不改变后续流式落盘。
            prefix = await f.read(16)
            await f.seek(0)
            if not _signature_valid(ftype, prefix):
                signature_invalid.append(raw_name)
                continue
            doc_id = str(uuid.uuid4())
            fpath = _doc_file_path(real_pid, doc_id, fname)
            # ✅ 优化：流式落盘（分块），旧实现 `await f.read()` 会把整份文件
            #    读入内存——多文件同时上传时峰值 = N × 30MB，易触发 OOM。
            size = await _stream_to_disk(f, fpath, MAX_UPLOAD_BYTES)
            if size < 0:
                oversize.append(raw_name)
                continue
            if size == 0:
                # ✅ 修复：0 字节文件既占档案位又注定解析失败，直接拒绝并说明
                try:
                    fpath.unlink()
                except OSError as e:
                    logger.warning(
                        "清理空文件 %s 失败（可能残留孤儿文件）: %s",
                        fpath, e, exc_info=True)
                empty_files.append(raw_name)
                continue
            # ✅ 配额：累计体积上限。已落盘文件需立即清理，避免孤儿文件。
            if total_bytes + size > MAX_UPLOAD_TOTAL_BYTES:
                try:
                    fpath.unlink()
                except OSError:
                    pass
                quota_files.append(raw_name)
                quota_exceeded = True
                # ✅ P2（2026-10-05 · 静默丢文件名）：`break` 不只停掉本文件，
                #    还会跳过**排在触发文件之后的全部剩余文件** —— 它们既不在
                #    too_many / oversize / unsupported 里，也不在 quota_files 里。
                #    实测：一次上传 10 个文件、第 3 个触顶时，响应里只点名 1 个，
                #    其余 6 个在任何字段里都不出现（用户以为传上去了，刷新后也
                #    找不到，且没有任何提示说明它们没被保存）。
                #    这些文件与触发文件同因（累计配额触顶后停止处理）未保存，
                #    一并登记进 quota_files —— 既有的提示文案「超出部分未保存
                #    （…）」无需改动即可覆盖，前端 `quota_files` 消费逻辑不变。
                quota_files.extend((x.filename or "uploaded_file")
                                   for x in files[_idx + 1:])
                break
            total_bytes += size
            written.append(fpath)

            # 同名替换：先删旧记录，旧文件延迟到 DB commit 成功后清理。
            # 若后续文件/数据库失败，rollback 后旧记录和旧文件仍可用。
            cur = await db.execute(
                "SELECT id, file_path FROM project_documents "
                "WHERE project_id=? AND file_name=?",
                (real_pid, fname))
            # ✅ P1（R13 漏改 · 2026-10-05）：返回 None 时下方 `cur.fetchall()`
            #    抛 AttributeError，被外层 except 接住 → rollback + 原样上抛 →
            #    用户拿到 500 且只有一句「上传保存失败」，看不出是数据库瞬时
            #    故障（可重试）还是自己的文件有问题。改抛 503（可重试）。
            #    外层 except 的回滚 + 已落盘文件清理仍会执行，不会留孤儿。
            if cur is None:
                logger.warning("同名文档查询失败（db.execute 返回 None），project=%s", real_pid)
                raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
            for old in await cur.fetchall():
                old_path = old["file_path"] if "file_path" in old.keys() else ""
                safe_old = _managed_upload_path(old_path)
                if safe_old and safe_old != fpath:
                    old_paths_to_delete.append(safe_old)
                elif old_path:
                    logger.warning("跳过上传目录外旧文件清理: %s", old_path)
                await db.execute(
                    "DELETE FROM project_documents WHERE id=?", (old["id"],))
                purged_old_doc_ids.append((str(old["id"]), real_pid))
                replaced += 1
            # 落库（parsed_markdown 留空 = 待解析）
            # ✅ 增强：自动文件分类 + 文件大小记录（供前端「文件导入」Tab 展示）
            doc_category = _auto_classify_document(fname, ftype)
            await db.execute(
                "INSERT INTO project_documents "
                "(id, project_id, file_name, file_type, doc_type, parsed_markdown, file_path, "
                "doc_category, file_size) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (doc_id, real_pid, fname, ftype, "全局事实上传", "", str(fpath),
                 doc_category, size))
            # ✅ 四层存储（阶段1 原文层）：指纹入库 + meta 落盘 + 文档索引。
            #    失败只降级告警，不阻断既有上传主链路（DB 档案已建）。
            try:
                meta = await doc_pipeline.ingest_upload(
                    doc_id=doc_id, project_id=real_pid, file_name=fname,
                    file_type=ftype, saved_path=fpath, size=size,
                    doc_category=doc_category)
                if meta:
                    await db.execute(
                        "UPDATE project_documents SET file_hash_md5=?,"
                        " file_hash_sha256=?, parse_status='pending' WHERE id=?",
                        (meta.get("file_hash_md5", ""),
                         meta.get("file_hash_sha256", ""), doc_id))
            except Exception:
                logger.exception("文档 %s 原文层入库失败（不影响上传）", fname)
            saved.append({"id": doc_id, "file_name": fname, "size": size})
    except Exception:
        # ✅ BUG 修复（悬空事务）：循环中途抛异常时，此前执行的 DELETE（同名替换）
        #    与 INSERT（档案记录）已在连接的隐式事务中排布但【尚未 commit】。
        #    旧实现只清理磁盘文件、不 rollback —— 连接归还写池后被下一个请求复用，
        #    这些残留语句会随该请求的 commit 一并提交，产生"幽灵删除/幽灵新增"。
        try:
            await db.rollback()
        except Exception:
            # 连接已损坏（如 disk I/O）时忽略，交由连接池剔除逻辑处理
            pass
        # ✅ 修复：旧实现在循环中抛异常时，已写盘的文件没有对应 DB 记录，
        #    成为永久孤儿文件（占空间且无法在界面删除）。异常时统一清理。
        for p in written:
            try:
                p.unlink()
            except OSError:
                pass
        logger.exception("上传保存失败，已回滚 %d 个已落盘文件", len(written))
        raise

    await db.commit()
    # 新记录已提交后再删除旧文件；删除失败不影响已提交的数据库状态。
    for old_path in old_paths_to_delete:
        try:
            old_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("清理被替换的旧上传文件失败: %s (%s)", old_path, e)
    # 被替换旧文档的四层产物同步清理（与手动删除文档同一 purge 口径）。
    for old_doc_id, old_pid in purged_old_doc_ids:
        try:
            await doc_pipeline.purge_document(db, doc_id=old_doc_id,
                                              project_id=old_pid)
        except Exception:
            logger.warning("清理被替换旧文档 %s 的四层产物失败", old_doc_id)
    result: dict = {"ok": True, "saved": saved,
                    "saved_count": len(saved),
                    "replaced": replaced}
    warnings: list[str] = []
    if oversize:
        result["oversize"] = oversize
        warnings.append(
            f"以下文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 未保存：{', '.join(oversize)}")
    if unsupported:
        result["unsupported"] = unsupported
        warnings.append(
            f"以下文件格式不支持（支持 {', '.join(sorted(SUPPORTED_EXTENSIONS))}）：{', '.join(unsupported)}")
    if signature_invalid:
        result["signature_invalid"] = signature_invalid
        warnings.append("以下文件扩展名与文件头不匹配，未保存：" + ", ".join(signature_invalid))
    if empty_files:
        result["empty"] = empty_files
        warnings.append(f"以下文件为空（0 字节）已忽略：{', '.join(empty_files)}")
    if too_many:
        result["too_many"] = too_many
        warnings.append(
            f"单次最多上传 {MAX_UPLOAD_FILES_PER_REQUEST} 个文件，"
            f"以下 {len(too_many)} 个文件未处理：" + "、".join(too_many[:10]))
    if quota_exceeded:
        result["quota_exceeded"] = True
        # ✅ 独立字段：被累计体积上限拒绝的文件清单（勿混入 oversize）
        if quota_files:
            result["quota_files"] = quota_files
        warn_files = f"（{', '.join(quota_files[:10])}）" if quota_files else ""
        warnings.append(
            f"本次上传累计体积超过 {MAX_UPLOAD_TOTAL_BYTES // (1024 * 1024)}MB 上限，"
            f"超出部分未保存{warn_files}，请分批上传")
    if replaced:
        warnings.append(f"替换了 {replaced} 个同名旧文件")
    if warnings:
        result["warnings"] = warnings
    return result


async def _ingest_parsed_doc(db, doc_id: str, project_id: str, file_name: str,
                             elapsed: float, diag: dict,
                             warnings: list[str], reparse: bool) -> dict:
    """✅ 四层存储（阶段2+3）：解析成功后落盘解析层产物 + 分块入 doc_chunks。

    失败只告警不阻断主链路（DB parsed_markdown 才是下游消费的唯一硬依赖，
    磁盘四层产物可通过 reparse 重建）。返回落盘摘要（可能为空 dict）。
    """
    try:
        cur = await db.execute(
            "SELECT parsed_markdown, parse_version FROM project_documents"
            " WHERE id=?", (doc_id,))
        r = dict(await cur.fetchone() or {})
        # 首次解析 prev='' → v1；重解析（源文档此前已成功解析过）→ 代次递增。
        # ✅ BUG 修复：旧版文档的 parse_version 列可能为空（四层存储上线前解析的），
        #    重解析时若拿空串当 prev_version，bump 不出来会被当成【首次解析】
        #    重置回 v1 —— 代次回退，时效性/增量判定永久失真。
        #    重解析路径上空版本一律视作 v1，本次落 v2。
        prev_version = (r.get("parse_version") or "v1") if reparse else ""
        md = r.get("parsed_markdown") or ""
        return await doc_pipeline.ingest_parse_result(
            db, doc_id=doc_id, project_id=project_id, file_name=file_name,
            markdown=md, page_count=int(diag.get("page_count") or 1),
            parse_duration_s=elapsed, parse_engine=str(diag.get("file_type") or ""),
            warnings=warnings, prev_version=prev_version)
    except Exception:
        logger.exception("文档 %s 解析层/分块落盘失败（不影响解析结果）", file_name)
        return {}


# ✅ 上传解析模块并发守卫（2026-09-23）：同一文档可能被多个请求并发解析
#    （前端 parsingDocId/parsingDocs 仅防主路径；外部直连 / 多标签页 /
#    批量 parse-all 与单份 parse 同时触发会绕过前端守卫）。并发解析同一份文件
#    会导致：重复 OCR 资源争用（扫描件尤甚）、parsed_markdown 被后写者覆盖、
#    四层分块重复写入。逐文档 asyncio.Lock 串行化同文档解析；锁对象在解析
#    完成后释放引用，避免随文档数无限增长。
_doc_parse_locks: dict[str, asyncio.Lock] = {}


def _get_doc_parse_lock(doc_id: str) -> asyncio.Lock:
    lock = _doc_parse_locks.get(doc_id)
    if lock is None:
        lock = asyncio.Lock()
        _doc_parse_locks[doc_id] = lock
    return lock


@asynccontextmanager
async def _doc_parse_guard(doc_id: str):
    """✅ 性能稳定性（2026-09-23）：解析并发守卫 + 锁条目自动回收。

    逐文档串行化解析（防并发重复 OCR / 后写覆盖），并在锁释放后从
    `_doc_parse_locks` 字典删除该条目 —— 旧实现只增不删，长生命周期服务下
    随文档数无限增长（内存泄漏）。单线程事件循环中，`async with` 退出到
    `finally` 之间不会有其他协程插入，锁必然空闲；即便有等待者，其已持有
    锁对象引用，从字典移除只是撤掉"登记"，不影响等待者，故安全删除。
    """
    lock = _get_doc_parse_lock(doc_id)
    try:
        async with lock:
            yield
    finally:
        _doc_parse_locks.pop(doc_id, None)


async def _reconcile_parse_status(db, doc_id: str) -> bool:
    """✅ BUG 修复（2026-09-25，解析结果「看起来丢了」/ 状态错乱）：

    把「正文非空但 parse_status 未置为 success」的陈旧行修正为 success。

    成因：``project_documents.parse_status`` 由 ``db._migrate`` 以
    ``TEXT DEFAULT 'pending'`` 增量补列 —— 四层存储上线前就已解析好的存量
    文档，正文躺在 ``parsed_markdown`` 里，状态列却被回填成 'pending'。
    而 ``parse_document`` / ``parse_all_documents`` 的「已解析」短路分支
    只回 ``already_parsed`` / 直接 ``continue``，**从不修正状态列**，于是：

      · 前端 ``computeDocStats`` 把这类文档恒计为「待解析」；
      · 点「解析」→ 后端回「已解析过」但列表标签仍是「待解析」；
      · 点「解析全部」→ 后端回「所有文档均已解析」、什么都没改；
      · 唯一出口是「全部重解析」(force)，白白重跑一遍 OCR。

    本函数在短路命中时顺带修正状态列：单条条件 UPDATE、幂等（已 success
    不重复写）、失败只告警不阻断解析主链路。

    :return: 是否实际执行了修正（True = 本次把状态从非 success 改为 success）。
    """
    try:
        cur = await db.execute(
            "UPDATE project_documents SET parse_status='success' "
            "WHERE id=? AND parsed_markdown IS NOT NULL AND parsed_markdown<>'' "
            "AND (parse_status IS NULL OR parse_status<>'success')",
            (doc_id,))
        await db.commit()
        # ✅ 2026-09-30 收敛到 safe_rowcount 单一出口（R13）：旧写法
        #   int(getattr(cur, "rowcount", 0) or 0) 不会抛 AttributeError，但
        #   execute() 返回 None 时**静默返回 0、零日志** —— 正是 AGENTS.md
        #   §4.12 要求消除的「写操作没生效却无任何日志」。safe_rowcount 会
        #   打带操作名的 WARNING，且把负 rowcount 归一为 0。
        # ⚠️ 本函数契约是 `-> bool`（调用方用 truthiness、测试用 `is False`），
        #    故必须显式转 bool：safe_rowcount 返回 int，直接返回会破坏契约
        #    （0 is False 恒为 False → 调用方与断言双双失真）。
        return bool(safe_rowcount(cur, what="单文档 parse_status 修正为 success"))
    except Exception:
        logger.exception("修正文档 %s 的解析状态失败（可忽略）", doc_id)
        return False


async def _reconcile_parse_status_all(db, project_id: str) -> int:
    """项目级批量修正陈旧 parse_status（见 :func:`_reconcile_parse_status`）。

    用于 ``parse_all_documents`` 入口：**放在 SELECT 待解析列表之前**，
    这样"所有文档均已解析"的早退分支也不会漏掉这批行。单条条件 UPDATE，
    只动"正文非空 + 状态非 success"的行；返回值供响应回传修正条数。
    """
    try:
        cur = await db.execute(
            "UPDATE project_documents SET parse_status='success' "
            "WHERE project_id=? AND parsed_markdown IS NOT NULL "
            "AND parsed_markdown<>'' AND (parse_status IS NULL "
            "OR parse_status<>'success')",
            (project_id,))
        await db.commit()
        # ✅ 2026-09-30 同上：收敛到 safe_rowcount（R13 静默失败 → WARNING）
        return safe_rowcount(cur, what="项目级 parse_status 批量修正")
    except Exception:
        logger.exception("批量修正项目 %s 的解析状态失败（可忽略）", project_id)
        return 0


async def _mark_parse_failed(db, doc_id: str, reason: str) -> None:
    """✅ 解析失败可观测性（2026-09-23）：把 parse_status 置为 'failed' 并把失败
    原因写入 parse_warnings 持久化，使文档列表能区分"待解析"与"解析失败"。

    旧实现解析失败时既不写状态、也不留痕 —— 文档永远停留在 'pending'，前端
    恒显"待解析"，用户无从得知已失败、会反复重试同一份文件。失败原因随列表
    回传，前端可展示"⚠ 解析失败"标签与具体原因。

    失败写入独立于主解析链路；即便写入本身异常也不影响已经抛出的解析错误。
    """
    try:
        await db.execute(
            "UPDATE project_documents SET parse_status='failed', "
            "parse_warnings=? WHERE id=?",
            (dump_parse_warnings([f"解析失败：{reason}"]), doc_id))
        await db.commit()
    except Exception:
        logger.exception("标记文档 %s 解析失败状态失败（可忽略）", doc_id)


async def _persist_parse_result(
    db,
    doc_id: str,
    stored: str,
    elapsed: float,
    warnings: list,
    diag: dict,
    truncated: bool,
) -> bool:
    """✅ 解析成功结果的唯一落库口径（重构 2026-10-04）。

    单文档解析与「解析全部」此前各写了一份字段完全相同的 UPDATE，历史上二者
    已多次发生口径漂移（parse_time / parse_truncated / 截断判定都曾不一致，
    对应「解析用时为空」「截断静默漏报」等已修复缺陷）。抽到此处后，写库字段
    只此一份，两条链路天然保持一致：
      - parsed_markdown：已按 MAX_PARSED_CHARS 截断的文本；
      - parse_time：解析耗时；parse_warnings：诊断告警（含截断说明）；
      - file_type：解析器按文件头嗅探的真实类型，空值不覆盖原值；
      - parse_status='success'：独立于四层入库成败，杜绝状态矛盾；
      - parse_truncated：字数截断或解析器级截断（PDF 截页/表格行数截断）。

    ✅ P1（R13 写路径漏改 · 2026-10-05）：本函数是**解析正文唯一的落库点**，
       返回值此前被丢弃 —— `db.execute()` 返回 None（R13）时 UPDATE 没执行，
       调用方照样往下走 `_ingest_parsed_doc`（它从库里读到空 parsed_markdown
       → 四层落空）、照样回 `{"ok": True, "text_len": N}`，**用户看着「解析
       成功、N 字」，刷新后列表里该文档仍是「待解析」、正文为 0 字**，且日志
       无任何痕迹。现改为显式返回是否写入成功，由调用方决定失败语义：
       单份解析 → 503（明确告知重试）；批量解析 → 计入 failed 并落
       parse_status='failed'（与其它失败原因同口径）。
    """
    cur = await db.execute(
        "UPDATE project_documents SET parsed_markdown=?, parse_time=?, parse_warnings=?,"
        " file_type=COALESCE(NULLIF(?, ''), file_type), parse_status='success',"
        " parse_truncated=? WHERE id=?",
        (stored, elapsed, dump_parse_warnings(warnings),
         str((diag or {}).get("file_type") or "").strip().lower(),
         int(bool(truncated)), doc_id))
    if cur is None:
        logger.warning(
            "解析结果落库未生效（db.execute 返回 None，R13），正文未写入：%s", doc_id)
        # ⚠️ 不 commit：没有成功的语句要提交，提交只会把上一次语句的残留
        #    事务上下文固化（见 doc_pipeline.py:322 同款告警的理由）。
        return False
    await db.commit()
    return True


async def _mark_project_facts_stale(db, project_id: str) -> int:
    """资料来源发生变化后，保守标记项目下全部事实为 stale。

    当前事实表尚未逐事实保存 source_doc_id，精确血缘无法可靠反查；项目级失效
    会在多方案共享事实场景下偏保守，但能确保正文/导出不继续使用旧解析结果。
    重新提取或人工改值/裁决后对应行可解除标记。
    """
    if not project_id:
        return 0
    cur = await db.execute(
        "UPDATE global_facts SET is_stale=1, "
        "updated_at=datetime('now','localtime') WHERE project_id=? AND is_stale=0",
        (project_id,))
    return safe_rowcount(cur, what="项目级事实批量置 stale")


async def _invalidate_project_export_caches(db, project_id: str) -> None:
    """✅ 跨模块链路收敛（2026-09-23）：删除 / 强制重解析文档后，失效该项目下所有方案的
    导出缓存，避免下游导出陈旧 docx。

    导出缓存默认关闭（O11），无缓存行时 `invalidate_export_cache` 为 no-op，安全；
    即便开启，也只删 export_cache 行 + 磁盘产物（不回滚已生成正文，既定设计）。
    按 scheme 失效：文档属项目级，项目下可能有多个方案，需逐一失效。
    """
    try:
        cur = await db.execute(
            "SELECT id FROM schemes WHERE project_id=?", (project_id,))
        sids = [r["id"] for r in await cur.fetchall()]
        for sid in sids:
            # 本路径的上游 _mark_project_facts_stale 刚把项目下全部事实置 stale，
            # 属于「事实已变更」→ 同步推进章节失效标记的时间戳。
            await invalidate_export_cache(db, sid, facts_touched=True)
    except Exception:
        logger.exception("失效项目 %s 导出缓存失败（可忽略）", project_id)


@router.post("/documents/{doc_id}/parse")
async def parse_document(doc_id: str, force: bool = False, db=Depends(get_db)):
    """② 解析单份文档：读取原始文件 → parse_file_content → 落库

    force=true 时忽略"已解析"直接重解析：用于早期版本受 80000 字上限
    截断的文档，重解析后可按当前上限（400000 字）获得完整内容。
    """
    import time as _time
    _parse_start = _time.time()
    cur = await db.execute(
        "SELECT file_name, file_path, parsed_markdown, project_id, parse_version "
        "FROM project_documents WHERE id=?", (doc_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    # ✅ 2026-09-23 日志埋点：解析入口绑定 project/doc 上下文，使本次解析及后续
    #    四层落盘 / 提取的日志都能凭 trace_id + doc 串联。
    set_context(trace_id=new_trace_id(),
                project_id=(row["project_id"] or "") if "project_id" in row.keys() else "",
                doc_id=doc_id)
    fname = row["file_name"]
    fpath = row["file_path"] if "file_path" in row.keys() else ""
    safe_path = _managed_upload_path(fpath)
    if fpath and safe_path is None:
        raise HTTPException(400, "文档路径不在事实上传目录内，请重新上传")
    # ✅ 并发守卫（2026-09-23）：锁内串行化同文档解析，并在锁内复核"已解析"
    #    状态 —— 否则两个并发请求都会在各自读到的 parsed_markdown='' 上各解析
    #    一次（重复 OCR + 后写覆盖）。锁内再查一次，已解析且非强制则直接返回。
    #    解析结束（含异常）后由 _doc_parse_guard 自动回收锁字典条目（防内存泄漏）。
    async with _doc_parse_guard(doc_id):
        cur = await db.execute(
            "SELECT parsed_markdown FROM project_documents WHERE id=?", (doc_id,))
        # ✅ P1（R13 漏改 · 2026-10-05）：读路径判空。直接 fetchone →
        #    AttributeError → 500（且用户只看到「解析失败」的裸错误，看不出是
        #    数据库瞬时故障还是文件问题）。503 = 可重试，与 _load_doc 同口径。
        if cur is None:
            logger.warning("读取解析状态失败（db.execute 返回 None），doc=%s", doc_id)
            raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
        _cur = await cur.fetchone()
        if _cur and _cur["parsed_markdown"] and not force:
            # ✅ BUG 修复（2026-09-25）：短路返回前先自修复陈旧的 parse_status
            #    （存量文档正文非空但状态仍为 pending → 前端恒显「待解析」、
            #    点解析永远「已解析过」却无任何变化，体感「解析结果丢失」）。
            reconciled = await _reconcile_parse_status(db, doc_id)
            result = {"ok": True, "already_parsed": True,
                      "parse_status": "success",
                      "text_len": len(_cur["parsed_markdown"])}
            if reconciled:
                result["reconciled"] = True
            return result
        if not safe_path or not safe_path.exists():
            raise HTTPException(400, f"原始文件缺失（{fname}），请重新上传")
        # 本次是否为重解析（源文档此前已有解析内容）—— 解析代次递增判定
        _was_parsed = bool(_cur and _cur["parsed_markdown"])
        try:
            content = await asyncio.to_thread(safe_path.read_bytes)
            text, diag = await asyncio.to_thread(
                parse_file_content_ex, content, fname)
        except ParseError as e:
            # ParseError 的消息是给用户看的（缺依赖 / 无 OCR 引擎 / 压缩炸弹），
            # 属于可操作提示，原样返回。
            logger.warning("文档 %s 解析失败: %s", fname, e)
            # ✅ 解析失败可观测性：标记 failed 状态 + 持久化原因（前端区分"待解析"）
            await _mark_parse_failed(db, doc_id, str(e))
            raise HTTPException(400, f"解析失败：{e}")
        except Exception:
            # ✅ 其它异常（库版本、路径、编码等）只进日志，不回传细节，避免泄露环境信息
            logger.exception("文档 %s 解析出现未预期异常", fname)
            await _mark_parse_failed(db, doc_id, "文件无法识别或已损坏")
            raise HTTPException(400, "解析失败：文件无法识别或已损坏，请查看服务端日志")
        if len(text.strip()) < 10:
            await _mark_parse_failed(
                db, doc_id,
                "未解析到有效文本（空白文件 / 扫描件无 OCR / 已损坏）")
            raise HTTPException(400,
                f"「{fname}」未解析到有效文本：可能是空白文件、扫描件或已损坏。"
                f"若为扫描件/图片，请确认 OCR 引擎可用"
                f"（打开 /api/v1/diagnostics/capabilities 查看启用方法）")
        # ✅ BUG 修复：落库长度与实际存储长度一致，避免前端显示"文本长度 X"
        # 但库里只存了 Y < X 的假象。超限时返回 truncated=true 让前端提示重解析。
        # ✅ 两类截断都要告知：① 落库字数超上限；② 解析器自身截断（PDF 页数 /
        #    表格行数）。旧实现只认第 ① 类，"第 50 页之后的设计参数没进事实库"
        #    这类问题在界面上完全无提示。
        char_truncated = len(text) > MAX_PARSED_CHARS
        stored = text[:MAX_PARSED_CHARS]
        # ✅ 增强：记录解析耗时（供前端显示「解析用时」诊断）
        _elapsed = round(_time.time() - _parse_start, 2)
        # ✅ 增强：解析器诊断告警持久化（截断 / OCR 兜底 / 加密 PDF）。
        #    旧实现告警只在本次响应里出现，刷新后丢失 —— 用户事后查看文档列表
        #    无从得知"该文档内容不完整"。落库后由 /documents 随列表回传。
        parser_warnings = [str(w) for w in (diag.get("warnings") or [])]
        if char_truncated:
            parser_warnings.append(
                f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限")
        # ✅ 解析成功即置 parse_status='success'（2026-09-23）：旧实现依赖四层入库
        #    （_ingest_parsed_doc）成功才写该列；一旦四层落盘失败，列仍为 'pending'
        #    而 parsed_markdown 已填充 → 状态矛盾，下游 doc_pipeline 去重/结构化
        #    读取会误判"未解析"，造成「解析结果丢失」隐患。此处让状态列与真实
        #    解析结果一致，独立于四层入库成败。
        # ✅ F2（2026-09-26）：字数截断与解析器级截断（PDF 截页/表格截行）一并
        #    持久化到 parse_truncated，使列表接口能直接读取、刷新后不漏报。
        truncated = char_truncated or bool(diag.get("truncated"))
        # ✅ 重构：成功结果统一走 _persist_parse_result，与「解析全部」同口径
        # ✅ P1（R13 写路径 · 2026-10-05）：落库未生效时**不能**继续 —— 后面的
        #    _ingest_parsed_doc 会从库里读到空正文、result 也会回 ok=True+text_len，
        #    用户看到「解析成功 N 字」而刷新后是 0 字。503 让用户明确重试。
        if not await _persist_parse_result(
                db, doc_id, stored, _elapsed, parser_warnings, diag, truncated):
            raise HTTPException(
                503, "解析结果保存失败（数据库暂时不可用），请稍后重试")
        # ✅ 四层存储（阶段2+3）：解析层产物落盘 + 分块入库（失败不阻断解析主结果）
        layer_info = await _ingest_parsed_doc(
            db, doc_id, row["project_id"], fname, _elapsed, diag,
            parser_warnings, _was_parsed)
        logger.info("文档 %s 解析完成：%d 字%s%s", fname, len(stored),
                    "（已截断至上限）" if char_truncated else "",
                    "（解析器截断）" if diag.get("truncated") else "")
        result: dict = {"ok": True, "text_len": len(stored), "truncated": truncated}
        if layer_info:
            result["layers"] = layer_info
        if parser_warnings:
            result["warnings"] = parser_warnings
        if char_truncated:
            result["warning"] = (f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限；"
                                 "如需完整提取请拆分文件后重新上传")
        if force:
            # 强制重解析改变了源文本：旧事实立即退出目录/正文/导出生成链路。
            stale_count = await _mark_project_facts_stale(db, row["project_id"])
            await db.commit()
            # ✅ 跨模块链路收敛：强制重解析改变了源文本 → 失效该项目所有方案的
            #    导出缓存，避免下游导出陈旧 docx（导出缓存默认关闭时为 no-op）。
            await _invalidate_project_export_caches(db, row["project_id"])
            result["stale_facts"] = stale_count
            result["note"] = (f"文档已重新解析，{stale_count} 条旧事实已停止注入；"
                              "请重新执行「AI 提取事实」更新全局事实")
        return result


@router.post("/documents/parse-all")
async def parse_all_documents(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    force: bool = False,
    db=Depends(get_db),
):
    """② 批量解析：解析该项目下所有未解析的文档

    ✅ 增强：force=true 时【连同已解析文档一起重解析】。用于两类场景：
      1. 早期版本按 80000 字上限截断的旧文档，重解析后可按当前上限补齐；
      2. 中途启用了 OCR 引擎后，重解析可让此前识别失败的扫描件重新获得文本。
    """
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    # ✅ 2026-09-23 日志埋点：批量解析入口绑定 project 上下文，便于按 trace_id 串联
    #    本次批量解析涉及的所有文档。
    set_context(trace_id=new_trace_id(), project_id=real_pid, scheme_id=scheme_id)
    # ✅ BUG 修复（2026-09-25，「解析全部」体感失效）：先做一轮状态自修复。
    #    存量文档正文非空但 parse_status 仍是 pending（四层存储上线前的默认值
    #    回填）时，它们**不进**下方 pending 列表 → 直接命中"所有文档均已解析"
    #    早退，用户看到的还是「待解析」。放在 SELECT 之前使两个分支都正确。
    reconciled_count = await _reconcile_parse_status_all(db, real_pid)
    sql = ("SELECT id, project_id, file_name, file_path FROM project_documents "
           "WHERE project_id=?")
    if not force:
        sql += " AND (parsed_markdown='' OR parsed_markdown IS NULL)"
    # ✅ 顺序固定：批量解析进度与失败列表按上传先后给出，避免 SQLite 返回顺序漂移
    sql += " ORDER BY created_at, id"
    cur = await db.execute(sql, (real_pid,))
    # ✅ P1（R13 漏改 · 2026-10-05）：读路径判空。降级成空 pending 列表会回
    #    「所有文档均已解析」—— 对着一个根本没读到的库宣布全解析完毕，是比
    #    500 更糟的假结论。503 让前端可重试。
    if cur is None:
        logger.warning("查询待解析文档失败（db.execute 返回 None），project=%s", real_pid)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    pending = [dict(r) for r in await cur.fetchall()]
    for _p in pending:
        _p["force_reparse"] = bool(force)
    if not pending:
        _msg = "所有文档均已解析"
        if reconciled_count:
            _msg += (f"（已修正 {reconciled_count} 份存量文档的解析状态）")
        return {"ok": True, "parsed": 0, "failed_count": 0, "failed": [],
                "truncated_count": 0, "truncated": [],
                # ✅ 响应形状一致（2026-09-25）：早退分支与主分支都带
                # reconciled/reconciled_count，前端才能用统一判据做提示。
                "reconciled_count": reconciled_count, "reconciled": [],
                "message": _msg}

    import time as _time

    parsed, failed = 0, []
    truncated_files: list[dict] = []
    reconciled: list[str] = []
    for doc in pending:
        # ✅ 并发守卫（2026-09-23）：逐文档锁串行化，防与并发单份 parse / 另一
        #    次 parse-all 重复解析同一文档（重复 OCR + 后写覆盖）。锁内先复核
        #    "已解析"状态，已解析且非强制则跳过（与单份解析同口径）。解析结束
        #    后由 _doc_parse_guard 自动回收锁字典条目（防内存泄漏）。
        async with _doc_parse_guard(doc["id"]):
            try:
                cur = await db.execute(
                    "SELECT parsed_markdown FROM project_documents WHERE id=?", (doc["id"],))
                _c = await cur.fetchone()
                if _c and _c["parsed_markdown"] and not force:
                    # ✅ BUG 修复（2026-09-25）：与单份解析同口径，短路前自修复
                    #    陈旧的 parse_status —— 否则「解析全部」对存量文档回
                    #    「所有文档均已解析」却不改状态，前端列表恒显「待解析」。
                    if await _reconcile_parse_status(db, doc["id"]):
                        reconciled.append(doc["file_name"])
                    continue
            except Exception as e:
                # ✅ A-4（2026-10-01）：原为 `except Exception: pass`，完全静默。
                #    该分支是「批量解析前复核该文档是否已解析」——DB 瞬时故障时会
                #    把**已解析**文档误判为未解析并重跑一遍（对大 PDF 意味着重复
                #    且昂贵的 OCR/MinerU 调用），事后却没有任何痕迹可查。
                logger.warning(
                    "复核文档 %s 解析状态失败（按未解析继续，可能重复解析）: %s",
                    doc.get("file_name", doc.get("id", "")), e, exc_info=True)
            try:
                _parse_start = _time.time()
                fpath = doc.get("file_path", "")
                safe_path = _managed_upload_path(fpath)
                if not safe_path:
                    # ✅ BUG 修复（2026-09-24）：路径不安全/原文件缺失/有效文本不足三个
                    #    分支此前只 append failed 后 continue，未落 parse_status='failed'，
                    #    文档永久停留 pending、前端恒显「待解析」（与单文档解析口径不一致）。
                    _reason = "原始文件路径不安全或缺失"
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                if not safe_path.exists():
                    _reason = "原始文件缺失"
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                content = await asyncio.to_thread(safe_path.read_bytes)
                text, diag = await asyncio.to_thread(
                    parse_file_content_ex, content, doc["file_name"])
                if len(text.strip()) < 10:
                    _reason = ("未解析到有效文本（空白/扫描件/损坏）；"
                               "扫描件需 OCR 引擎，见 /api/v1/diagnostics/capabilities")
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                # ✅ BUG 修复：截断是「解析成功但内容不完整」的告警，不应计入 failed，
                #    否则前端提示"失败 N 个"会让用户误以为整份解析失败。
                char_truncated = len(text) > MAX_PARSED_CHARS
                stored = text[:MAX_PARSED_CHARS]
                # ✅ 增强：解析器诊断告警随解析结果持久化（与单文档解析一致）
                all_warnings = [str(w) for w in (diag.get("warnings") or [])]
                if char_truncated:
                    all_warnings.append(
                        f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限")
                # ✅ BUG 修复（口径不一致）：单文档解析会写 parse_time，批量解析不写，
                #    于是「解析全部」之后前端列表的「解析用时」恒为空 —— 用户无法
                #    判断到底是哪份文件拖慢了整批解析。现与单文档解析保持一致。
                _elapsed = round(_time.time() - _parse_start, 2)
                # ✅ 解析成功即置 parse_status='success'（与单份解析同口径，2026-09-23）：
                #    消除四层入库失败导致 parse_status 陈旧、下游误判"未解析"。
                # ✅ F2（2026-09-26）：与单文档解析一致，持久化截断标记
                doc_truncated = char_truncated or bool(diag.get("truncated"))
                # ✅ 重构：成功结果统一走 _persist_parse_result，与单文档解析同口径
                # ✅ P1（R13 写路径 · 2026-10-05）：落库未生效时不得计入 parsed ——
                #    否则响应报「成功 N 份」而库里正文是空的（假成功）。归入 failed
                #    并落 parse_status='failed'，与其它失败原因同口径、可重试。
                if not await _persist_parse_result(
                        db, doc["id"], stored, _elapsed, all_warnings, diag,
                        doc_truncated):
                    _reason = "解析结果保存失败（数据库暂时不可用），请重试"
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                # ✅ 四层存储（阶段2+3）：逐文档落盘解析层 + 分块（失败不阻断批量）
                await _ingest_parsed_doc(
                    db, doc["id"], doc["project_id"], doc["file_name"],
                    _elapsed, diag, all_warnings,
                    bool(doc.get("force_reparse")))
                parsed += 1
                # ✅ BUG 修复（2026-09-25，口径不一致）：单文档解析的截断判定是
                #    `char_truncated or bool(diag["truncated"])`，本函数旧实现多加了
                #    一个 `and diag["warnings"]` 条件 —— 解析器标记了 truncated
                #    但 warnings 为空时（例如仅页数截断、未附告警文本）批量解析
                #    **静默漏报**，用户以为拿到了全文。现与单份解析完全对齐。
                doc_truncated = char_truncated or bool(diag.get("truncated"))
                if doc_truncated:
                    if char_truncated:
                        truncated_files.append({
                            "file_name": doc["file_name"],
                            "reason": f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限（建议拆分文件后重新上传）",
                        })
                    else:
                        truncated_files.append({
                            "file_name": doc["file_name"],
                            "reason": ("；".join(str(w) for w in diag["warnings"])
                                       if diag.get("warnings")
                                       else "解析器判定内容不完整（PDF 页数/表格行数截断）"),
                        })
            except ParseError as e:
                logger.warning("文档 %s 批量解析失败: %s", doc["file_name"], e)
                # ✅ 解析失败可观测性：标记 failed 状态（前端区分"待解析"）
                await _mark_parse_failed(db, doc["id"], str(e)[:120])
                failed.append({"file_name": doc["file_name"], "reason": str(e)[:120]})
            except Exception:
                logger.exception("文档 %s 批量解析出现未预期异常", doc["file_name"])
                await _mark_parse_failed(db, doc["id"], "文件无法识别或已损坏")
                failed.append({"file_name": doc["file_name"],
                               "reason": "文件无法识别或已损坏，请查看服务端日志"})
    # ✅ 循环内已对每个成功文档逐条 commit（避免长事务锁库），此处无需再 commit；
    #    旧实现的尾部重复 commit 属无谓 IO 且造成「事务已完成」的误导语义。

    result: dict = {"ok": True, "parsed": parsed, "failed_count": len(failed),
                    "failed": failed,
                    "truncated_count": len(truncated_files),
                    "truncated": truncated_files,
                    "force": force,
                    # ✅ 2026-09-25：本轮自修复的陈旧 parse_status 条数（0 表示无）
                    "reconciled_count": reconciled_count + len(reconciled),
                    "reconciled": reconciled}
    if force and parsed:
        stale_count = await _mark_project_facts_stale(db, real_pid)
        await db.commit()
        # ✅ 跨模块链路收敛：强制重解析整批文档 → 失效该项目所有方案导出缓存
        #    （避免下游导出陈旧 docx；导出缓存默认关闭时为 no-op）。
        await _invalidate_project_export_caches(db, real_pid)
        result["stale_facts"] = stale_count
        result["note"] = (f"已强制重新解析，{stale_count} 条旧事实已停止注入；"
                          "请重新执行「③ AI 提取事实」")
    warnings: list[str] = []
    if failed:
        warnings.append(
            f"{len(failed)} 份文档解析失败：" + "；".join(
                f"{f['file_name']}（{f['reason']}）" for f in failed[:5]))
    if truncated_files:
        warnings.append(
            f"{len(truncated_files)} 份文档内容超长被截断：" + "；".join(
                f"{f['file_name']}（{f['reason']}）" for f in truncated_files[:5]))
    if warnings:
        result["warnings"] = warnings
    return result


# =========================================================================
# ✅ 文件导入/解析模块增强：自动分类 + 文件预览 API
# =========================================================================

# ✅ 口径收敛（2026-09-25）：分类清单与关键词规则的**唯一事实源**已迁到
# ``services/doc_categories.py``（该模块同时被 bid_analysis 的提取优先级复用）。
# 此处保留 `_DOC_CATEGORY_KEYWORDS` 名称作为**只读别名**，兼容既有调用方与
# 单测的引用习惯；新代码请直接用 doc_categories.AUTO_CLASSIFY_RULES。
_DOC_CATEGORY_KEYWORDS = tuple(AUTO_CLASSIFY_RULES)


def _auto_classify_document(file_name: str, file_type: str = "") -> str:
    """根据文件名自动判断文档分类（用于「文件导入」Tab 展示）。

    ✅ 口径收敛（2026-09-25）：分类清单与关键词规则的**唯一事实源**已迁到
    ``services/doc_categories.py`` —— 旧实现把 9 类关键词只写在本文件里，
    而 ``bid_analysis._combine_doc_texts`` 另写了一份只覆盖 6 类的优先级表，
    三处口径彼此漂移（漏改「资质材料/人员资料/财务资料/业绩证明」→ 命中后
    被 AI 提取静默排到最后）。现只做委托，保留原函数名兼容既有调用与单测。
    """
    return auto_classify_document(file_name, file_type)


@router.get("/documents/{doc_id}/preview")
async def preview_document(doc_id: str, max_chars: int = Query(5000, ge=100, le=50000),
                              db=Depends(get_db)):
    """文件预览：返回已解析 Markdown 的前 N 个字符供前端快速浏览。

    用于「文件解析」Tab 的「查看解析内容」弹窗——**不下载整份文件**，
    避免大文件（20 万+ 字）把前端内存打爆。
    """
    cur = await db.execute(
        "SELECT id, file_name, file_type, parsed_markdown, doc_category, "
        "file_size, parse_time, parse_warnings, parse_truncated, created_at "
        "FROM project_documents WHERE id=?",
        (doc_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    # ✅ BUG 修复（2026-09-18）：get_conn 的 row_factory 是 sqlite3.Row ——
    #    它支持 row["col"] 下标但**没有 .get() 方法**。旧实现用 row.get(...)
    #    使 /documents/{id}/preview 在文档已存在时必然抛 AttributeError → 500
    #    （HTTP 冒烟实测崩溃点）。
    row = dict(row)
    md = row["parsed_markdown"] or ""
    if not md:
        # ✅ BUG 修复（契约对称，2026-09-21）：未解析分支此前只回 6 个字段，
        #    缺 file_type / doc_category / file_size / parse_time / created_at /
        #    preview_truncated —— 前端用同一 interface 消费预览响应时，未解析
        #    文档拿到 undefined 会渲染出空白占位（如「格式：」空值）。
        #    现两条返回路径字段集合完全一致（由
        #    test_preview_response_contract_symmetric 钉住）。
        return {
            "doc_id": doc_id,
            "file_name": row["file_name"],
            "file_type": row["file_type"],
            "doc_category": row["doc_category"],
            "file_size": row["file_size"],
            "parse_time": row["parse_time"],
            "parse_warnings": _decode_parse_warnings(row.get("parse_warnings")),
            "created_at": row["created_at"],
            "preview": "",
            "text_len": 0,
            "is_parsed": False,
            "preview_truncated": False,
            "truncated": False,
            "message": "该文档尚未解析，请先执行解析",
        }

    total_len = len(md)
    preview = md[:max_chars]
    truncated = total_len > max_chars
    # ✅ 2026-09-26（口径区分，补齐 G2 可感知性缺口）：预览弹窗此前只有
    #    ``preview_truncated``（"预览只截取了前 N 字"），**没有解析级截断信号**。
    #    一份 PDF 因超 50 页被截断的文档，用户在预览里看到的正文与完整文档
    #    一模一样长（都远小于预览上限），于是以为资料齐全，实际上 18 项提取
    #    与正文生成都只看到了前 50 页。现新增 ``truncated`` 承载解析级截断
    #    （与 /documents 列表口径完全一致：持久化标记 + 字数上限双口径），
    #    两个字段各司其职、互不覆盖。
    #    向后兼容：新增字段，既有消费方（`preview_truncated`）语义不变。
    doc_truncated = bool(row.get("parse_truncated")) or _is_truncated(total_len)
    return {
        "doc_id": doc_id,
        "file_name": row["file_name"],
        "file_type": row["file_type"],
        "doc_category": row["doc_category"],
        "file_size": row["file_size"],
        "parse_time": row["parse_time"],
        "parse_warnings": _decode_parse_warnings(row.get("parse_warnings")),
        "created_at": row["created_at"],
        "preview": preview,
        "text_len": total_len,
        # ✅ 补齐契约对称：未解析分支回 is_parsed=False，已解析分支旧实现
        #    漏回该字段 —— 前端用 `data.is_parsed === true` 硬判断时会退到
        #    「未解析」分支。现两条返回路径字段集合一致。
        "is_parsed": True,
        "preview_truncated": truncated,
        "truncated": doc_truncated,
        "message": "（预览仅显示前 {0} 字，完整内容请到「结构化解析」中使用或重新解析）".format(max_chars) if truncated else "",
    }


@router.patch("/documents/{doc_id}/category")
async def update_document_category(doc_id: str, body: dict, db=Depends(get_db)):
    """手动修改文件分类（自动分类不准确时用户可手动调整）。

    ✅ 三侧口径对齐：分类值参与多处下游逻辑（提取侧按分类排优先级、
    展示侧按分类映射 Tag 颜色），写入未知值会让这些口径静默退化。
    故只接受 category-options 登记的合法分类（含「其他」）。
    """
    category = body.get("doc_category", "").strip()
    if not category:
        raise HTTPException(400, "doc_category 不能为空")
    valid_categories = {cat for cat, _ in _DOC_CATEGORY_KEYWORDS} | {"其他"}
    if category not in valid_categories:
        raise HTTPException(
            400, f"未知分类（{category}），可选："
            f"{', '.join(sorted(valid_categories))}")
    cur = await db.execute("SELECT id FROM project_documents WHERE id=?", (doc_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    await db.execute(
        "UPDATE project_documents SET doc_category=? WHERE id=?", (category, doc_id))
    await db.commit()
    return {"ok": True, "doc_id": doc_id, "doc_category": category}


@router.get("/documents/category-options")
async def list_category_options():
    """返回所有可用的文档分类选项（供前端下拉选择）。

    ✅ 口径收敛（2026-09-25）：选项与关键词规则统一取自
    ``services/doc_categories.py``（分类唯一事实源），不再在本文件散落维护。
    """
    return {
        "options": category_options(),
        "auto_keywords": [{"category": cat, "keywords": list(kws)}
                          for cat, kws in AUTO_CLASSIFY_RULES],
    }


# ✅ 提取入口：SSE /sse/generate-facts/{scheme_id}（见 sse_handlers.py）
