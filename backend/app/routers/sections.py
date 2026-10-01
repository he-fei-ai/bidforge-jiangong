"""章节管理路由"""
import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.db import get_db
from app.models import SectionCreate, SectionUpdate
from app.routers.review import reset_review_on_content_change  # ✅ G9：正文变更→退回待审核
from app.services.ai.json_response import (
    collect_json_response, renumber_outline, strip_outline_numbering,
)
from app.services.numbering import (
    normalize_section_content_subheadings,  # ✅ 正文子标题编号落库前规范化（唯一实现）
    renumber_section_outline_ids,  # ✅ 编号统一：DB 重排唯一实现
    validate_scheme_numbering_consistency,  # ✅ 编号统一：显式跨校验器
    repair_scheme_numbering_consistency,  # ✅ 编号统一：显式跨校验器（修复漂移）
    list_numbering_versions as _list_numbering_versions,  # ✅ 编号版本管理
    rollback_numbering_version as _rollback_numbering_version,  # ✅ 编号版本管理
)
from app.services.ai.provider_factory import chat_with_fallback
from app.services.ai.prompts._registry import render
from app.services.outline_utils import MAX_OUTLINE_DEPTH, normalize_outline

# ✅ 2026-09-30 深度上限文案单一来源：create_section 与 update_section（移动路径）
# 共用同一条错误文案，避免两处各自硬编码后再次分叉（本仓反复出现的根因模式）。
_DEPTH_EXCEEDED_MSG = (
    f"目录最多支持 {MAX_OUTLINE_DEPTH} 级，请在上一级章节内编写正文小节"
)
from app.services.content_polish import quality_issues
from app.services.content_shrink import (
    shrink_content_rounds, SHRINK_MAX_ROUNDS,
    SHRINK_SETTLE_RATIO,  # noqa: F401  — 兼容再导出（单测断言路由与服务同源）
)
from app.services.content_utils import (
    word_status_for, text_word_count, WORD_OVER_RATIO,
)
from app.routers._chart_pipeline import register_inline_charts  # ✅ B31：手动保存同步图表登记
from app.services.standards_registry import (
    STANDARD_DB_CHECKED_AT, STANDARD_DB_VERSION,
)

logger = logging.getLogger("sections")

# 字数压缩参数（SHRINK_MAX_ROUNDS / SHRINK_SETTLE_RATIO）的唯一来源是
# services/content_shrink.py —— 正文生成链路的「生成后自动压缩」也要用同一套值，
# 故上移服务层后此处仅 import，不再本地定义（避免两处漂移）。

router = APIRouter(prefix="/api/v1/schemes/{scheme_id}/sections", tags=["sections"])

# ✅ 编译状态（schemes.status）的目录侧语义常量（2026-09-21）：
# 「目录待生成」= 方案当前没有任何目录章节（新建 / 被清空）。
# 旧实现清空目录时仍写「目录已确认」，与前端头部 Tag、review 端点的
# scheme_status 展示口径冲突（详见 _save_outline_to_db 空目录分支注释）。
# 非空目录落库后仍统一置「目录已确认」（语义正确，保持不变）。
OUTLINE_EMPTY_STATUS = "目录待生成"
OUTLINE_SAVED_STATUS = "目录已确认"


async def _build_tree(db, scheme_id: str, include_content: bool = False) -> list:
    # ✅ 性能优化：树构建默认不拉 content 大字段（单章可达数 KB，全方案 MB 级传输浪费）；
    # 仅正文工作台列表需要 content 时传 include_content=True
    if include_content:
        cols = "sec.*"
    else:
        cols = (
            "sec.id, sec.scheme_id, sec.project_id, sec.parent_id, sec.title,"
            "sec.description, sec.level, sec.status, sec.word_count, sec.word_budget,"
            "sec.word_status, sec.outline_json, sec.review_status, sec.sort_order,"
            "sec.locked,"
            "sec.generation_standard, sec.last_generation_standard")
    # ✅ 2026-09-29：章节「事实变更失效标记」（facts_stale）。
    # 背景：全局事实变更后，已生成的正文仍是生成当时的事实快照，导出缓存也会被
    # 清空，但**没有任何信号告知用户「这一章引用的事实已经变了」**—— 用户可能
    # 带着过时参数交付方案。此前设计文档把它归为「需单立子项」，本轮以最小侵入
    # 方式落地：**只提示、绝不静默重写**用户已编辑的正文。
    # 为什么用「方案级单点时间戳 + 读侧派生」而不是给 sections 加布尔列：
    #   · 写侧只有 1 处（invalidate_export_cache(..., facts_touched=True)），
    #     而正文写路径有 13 处 —— 若各写一个布尔列，「漏改一处」就会让
    #     重生后的章节被永久标成过时（本仓反复踩的同类陷阱）；
    #   · 读侧派生天然自愈：任何一次正文重写都会推进 sections.updated_at，
    #     标记自动消失，无需任何额外清理代码。
    # 判定链（顺序即优先级，命中任一分支即判 0「不标记」）：
    #   1. 章节无正文 → 没有可过时的内容；
    #   2. 方案无事实变更时间戳 → 旧数据无从判定，不打扰用户（向后兼容）；
    #   3. 章节 updated_at 无法解析 → 无法比较，按不标记处理（fail-soft）；
    #   4. 事实变更早于或等于章节写入 → 正文已包含最新事实。
    # 时间字符串格式并不统一（Python `datetime.now().isoformat()` 用 'T' + 微秒，
    # SQLite `datetime('now','localtime')` 用空格），故统一交给 SQLite 的
    # datetime() 归一化，**绝不直接做字符串比较**。
    cur = await db.execute(
        f"SELECT {cols},"
        " CASE WHEN COALESCE(sec.content, '') = '' THEN 0"
        "      WHEN COALESCE(sch.facts_updated_at, '') = '' THEN 0"
        "      WHEN datetime(sec.updated_at) IS NULL THEN 0"
        "      WHEN datetime(sch.facts_updated_at)"
        "           <= datetime(sec.updated_at) THEN 0"
        "      ELSE 1 END AS facts_stale"
        " FROM sections sec"
        " LEFT JOIN schemes sch ON sch.id = sec.scheme_id"
        " WHERE sec.scheme_id=?"
        " ORDER BY sec.sort_order, sec.created_at, sec.id", (scheme_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    nodes = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for nid, n in nodes.items():
        pid = n.get("parent_id")
        if pid and pid in nodes:
            nodes[pid]["children"].append(n)
        else:
            roots.append(n)

    # ✅ 确定性排序（2026-09-27）：兄弟章节 sort_order 相同时（拖拽 /reorder 只带
    #    部分 id、外部脚本直改库、历史脏数据）旧实现只按 sort_order 排 —— 相等项的
    #    相对顺序取决于 SQL 返回顺序（rowid/执行计划），同一份数据不同次调用可能得到
    #    不同顺序，进而**编号漂移**（正文提示词里的「当前章节编号」与展示不一致）。
    #    现统一以 (sort_order, id) 做最终排序：id 是主键、全局唯一，等值组也有全序。
    #    注意 roots 同样要排 —— 旧实现只排 children，根节点顺序完全依赖 SQL。
    def _sort_key(n: dict):
        so = n.get("sort_order")
        try:
            so = int(so or 0)
        except (TypeError, ValueError):
            so = 0
        return (so, str(n.get("id") or ""))

    def _sort_children(nodes_list):
        nodes_list.sort(key=_sort_key)
        for n in nodes_list:
            if n["children"]:
                _sort_children(n["children"])

    _sort_children(roots)
    return roots


def _scheme_task_status_in_progress(scheme_id: str, task_type: str) -> str | None:
    """本方案是否有**仍在跑**（running / paused）的指定类型后台任务。

    返回命中的任务状态；无在跑任务返回 None。

    ✅ 状态感知是硬要求：内存态里可能残留**终态**条目（task_control 的 stop 走
    set_task_status 只改状态不 pop 内存态；finish_task 若在写库阶段抛异常也走
    不到 _tasks.pop）。终态条目绝不应阻塞用户操作 —— 宁可放行，不可误拦。
    脏数据（缺 status 字段）同样按终态处理。

    本函数是「竞态守卫」的唯一入口，各端点通过下方的语义化封装调用，
    禁止在端点内自己遍历 _tasks（历史上就因各写一份而口径漂移）。
    """
    from app.services.ai.task_registry import _tasks
    for state in _tasks.values():
        if (state.get("type") == task_type
                and state.get("scheme_id") == scheme_id
                and state.get("status") in ("running", "paused")):
            return str(state.get("status") or "running")
    return None


def content_generation_in_progress(scheme_id: str) -> str | None:
    """本方案是否有**仍在跑**的正文生成任务（409 竞态守卫的**唯一**入口）。

    返回命中的任务状态（running / paused），无则在跑任务返回 None。

    ✅ BUG 修复（G12-4 · 2026-09-20）：旧实现（update_section / reset_content 各写
    一份）只校验 type + scheme_id，**不校验状态** —— 于是内存态里残留的僵尸条目
    会让守卫永久误判「正在生成」：
      · `POST /task/{id}/control` 的 stop 走 `set_task_status`，只改状态、
        **不** pop 内存态；
      · `finish_task` 若在第 207 行 `get_conn()` / UPDATE / commit 阶段抛异常
        （连接池耗尽、磁盘满、并发写冲突），`_tasks.pop` 永远走不到；
      · 进程崩溃后重启的遗留任务也可能长期停在 running。
    任何一种情况都会造成本方案所有章节的**手工保存与重置正文永久 409**，
    用户表现为「正文生成早就停了，我却再也存不了任何一章」，只能重启后端。
    现只看真正在跑的任务；两处调用点也收口到同一函数，避免口径再次漂移。

    ✅ 调用点已扩至四处（2026-09-21）：update_section / reset_content（G12-4）
    之外，save-outline 与 outline-library/apply-and-save 同样会**整表重建 sections**，
    与 _persist_section 的无条件 UPDATE 直接冲突，共用同一守卫。
    """
    return _scheme_task_status_in_progress(scheme_id, "content_generation")


def outline_generation_in_progress(scheme_id: str) -> str | None:
    """本方案是否有**仍在跑**的目录生成任务（save-outline 409 守卫的唯一入口）。

    返回命中的任务状态（running / paused），无则在跑任务返回 None。

    ✅ BUG 修复（2026-09-21）：旧实现整条目录链路没有任务级竞态守卫 ——
    目录生成 SSE 协程在后台构建完整目录树并落 checkpoint，与此同时用户若在
    目录 Tab 点「保存目录 / 清除所有目录 / 从目录库套用」，会走到同一张
    sections 表：AI 生成完成后弹出一级目录确认闸门，用户确认即把刚手工保存的
    目录整表覆盖；反向地，用户保存后又继续等 AI 结果，两边互相覆盖且无任何提示。
    前端虽已用 `disabled={generating}` 挡住主路径，但接口层面必须有兜底
    （绕过 UI 的直连调用 / 多标签页并发 / 断线重挂后旧标签页仍在操作）。
    口径与 content_generation_in_progress 完全一致：只拦 running/paused。

    ✅ 调用点已扩至八处（2026-09-23 · 守卫缺口修复）：save-outline /
    apply-and-save / update_section / reset_content 之外，create_section /
    delete_section / reorder 四个单章节变更端点此前零守卫 —— 目录生成运行中
    增删改/拖拽，会在 AI 确认闸门整表重建时被静默覆盖（丢失更新），且与
    renumber_sections_after_reorder 构成同表写写竞态。现补齐同口径守卫。
    """
    return _scheme_task_status_in_progress(scheme_id, "outline_generation")


async def invalidate_consistency_scan_cache(db, scheme_id: str) -> None:
    """目录结构变更 / 改名 / 整表重建后，作废本方案的一致性扫描增量缓存。

    ✅ BUG 修复（2026-09-23 · 缓存未失效）：consistency_scan_cache 只按
    「章节正文指纹 + 上下文指纹」双键命中，目录结构（编号/层级/顺序/标题）
    不在键内 —— 拖拽、移动、改名、删章、重存目录后旧扫描行照样命中，
    增量优化退化为「回馈旧冲突」；且删章后缓存行成为孤儿残留。
    现所有结构变更入口统一按 scheme 清空（下次扫描全量重扫，
    只在结构操作时触发，代价可控）。

    失败只告警不阻断（与缓存读写降级口径一致：旧库可能尚未迁移出此表，
    表不存在时 DELETE 抛错被吞）。内部不 commit，由调用方统一提交。
    """
    try:
        await db.execute(
            "DELETE FROM consistency_scan_cache WHERE scheme_id=?", (scheme_id,))
    except Exception as e:
        logger.warning("一致性扫描缓存失效失败（scheme=%s，不阻断业务）: %s", scheme_id, e)


@router.get("")
async def list_sections(scheme_id: str, include_content: bool = True, db=Depends(get_db)):
    """章节树列表。

    include_content=False 时省略 content 大字段（P0-7 轮询瘦身）：
    生成期间前端每 3s 轮询只拉结构字段，正文增量由 SSE 事件推送。
    """
    tree = await _build_tree(db, scheme_id, include_content=include_content)
    # ✅ 返回 id / project_id（前端全局事实模块按 project_id 拉取资料文档列表）
    # ✅ 2026-09-26：generation_standard 随方案快照下发（章节「沿用方案（X）」回显）
    # ✅ 2026-09-29：随方案快照下发 facts_updated_at —— 前端据此显示
    # 「事实已变更，共 N 章正文可能过时」的方案级提示（N 由树内 facts_stale 汇总）。
    cur = await db.execute(
        "SELECT id, project_id, word_budget, word_count, status, generation_standard,"
        " facts_updated_at FROM schemes WHERE id=?",
        (scheme_id,))
    s = await cur.fetchone()
    return {"tree": tree, "scheme": dict(s) if s else None}


@router.get("/quality")
async def sections_quality(scheme_id: str, db=Depends(get_db)):
    """正文质量审计（交付前自检）。

    检测两类不可接受问题：
    1. 口语化 / AI 腔残留（正常情况下生成落库前已清洗，命中说明手工编辑过）；
    2. 引用已废止或被替代的标准编号（须替换为现行版本）。

    Returns:
        items：命中问题的章节；summary：汇总计数；standard_db_*：标准库版本信息。
    """
    cur = await db.execute(
        "SELECT id, title, content FROM sections"
        " WHERE scheme_id=? AND COALESCE(content,'')!='' ORDER BY sort_order",
        (scheme_id,))
    rows = [dict(r) for r in await cur.fetchall()]

    # ✅ BUG 修复（2026-09-19 · 承接遗留余项「quality /sections 同步扫描阻塞事件循环」）：
    #    quality_issues 是 CPU 密集的正则扫描（口语化规则 × 章节正文逐条匹配），
    #    大方案（百章 × 数千字 = MB 级正文）全量扫一遍可达数秒。旧实现直接在
    #    async 路由里同步执行，期间**整个事件循环被阻塞**——所有并发请求
    #    （含正在跑的生成任务 SSE 心跳、其它 API）集体卡顿。现把扫描整体挪到
    #    工作线程（asyncio.to_thread），事件循环只等待结果，DB 连接不受影响
    #    （rows 已提前物化为 dict，线程内不触碰 aiosqlite 连接）。
    def _scan() -> list[dict]:
        items: list[dict] = []
        for r in rows:
            issues = quality_issues(r.get("content") or "")
            if issues["colloquial_hits"] or issues["abolished_standards"]:
                items.append({
                    "section_id": r["id"],
                    "title": r["title"],
                    "colloquial_hits": issues["colloquial_hits"],
                    "abolished_standards": issues["abolished_standards"],
                })
        return items

    items = await asyncio.to_thread(_scan)

    return {
        "items": items,
        "summary": {
            "checked": len(rows),
            "problem_sections": len(items),
            "colloquial": sum(len(i["colloquial_hits"]) for i in items),
            "abolished_standards": sum(len(i["abolished_standards"]) for i in items),
        },
        "standard_db_version": STANDARD_DB_VERSION,
        "standard_db_checked_at": STANDARD_DB_CHECKED_AT,
    }


# ================================================================
# F-CONTENT-STANDARD(2026-09-26)：生成标准校验报告端点
# ================================================================

@router.get("/report/{section_id}")
async def section_generation_report(scheme_id: str, section_id: str, db=Depends(get_db)):
    """获取章节最近一次正文生成的校验报告（JSON）。

    Returns:
        section_id: 章节标识
        section_title: 章节标题
        generation_standard: 最近一次生效的生成标准（precise / fuzzy / ''=未记录）
        report: standard_report JSON（含 passed / issues / stats），
                若无历史记录则返回空结构 {passed: true, error_count: 0, ...}
    """
    import json as _json
    from app.services.content_standard import _empty_report

    cur = await db.execute(
        "SELECT id, title, generation_standard, last_generation_standard, "
        "       last_generation_report "
        "FROM sections WHERE scheme_id=? AND id=?",
        (scheme_id, section_id))
    row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "章节不存在")

    rep_json = row["last_generation_report"] or ""
    if rep_json:
        try:
            report = _json.loads(rep_json)
        except Exception:
            report = _empty_report(row["last_generation_standard"] or row["generation_standard"])
    else:
        report = _empty_report(row["last_generation_standard"] or row["generation_standard"])

    return {
        "section_id": row["id"],
        "section_title": row["title"],
        "generation_standard": row["generation_standard"],
        "last_generation_standard": row["last_generation_standard"],
        "report": report,
    }


@router.get("/report-summary")
async def scheme_report_summary(scheme_id: str, db=Depends(get_db)):
    """获取方案所有章节的生成标准校验汇总。

    Returns:
        total: 章节总数 / checked: 有生成记录的章节数
        by_standard: {precise: n, fuzzy: n}
        total_errors / total_warnings / total_issues
        issue_type_counts: {type: count}
        sections: [{section_id, title, standard, passed, errors, warnings}]
    """
    import json as _json
    from collections import Counter
    from app.services.content_standard import _empty_report

    cur = await db.execute(
        "SELECT id, title, generation_standard, last_generation_standard, "
        "       last_generation_report, status "
        "FROM sections WHERE scheme_id=? ORDER BY sort_order, created_at",
        (scheme_id,))
    rows = [dict(r) for r in await cur.fetchall()]

    by_std: Counter = Counter()
    issue_types: Counter = Counter()
    total_errors = total_warnings = total_issues = 0
    sections_out: list = []

    for r in rows:
        std = r["last_generation_standard"] or r["generation_standard"] or "precise"
        by_std[std] += 1
        rep_json = r["last_generation_report"] or ""
        if rep_json:
            try:
                rep = _json.loads(rep_json)
            except Exception:
                rep = _empty_report(std)
        else:
            rep = _empty_report(std)
        for it in rep.get("issues", []) or []:
            issue_types[it.get("type", "unknown")] += 1
        errs = rep.get("error_count", 0) or 0
        warns = rep.get("warning_count", 0) or 0
        total_errors += errs
        total_warnings += warns
        total_issues += errs + warns
        sections_out.append({
            "section_id": r["id"],
            "title": r["title"],
            "status": r.get("status", ""),
            "generation_standard": r["generation_standard"],
            "last_generation_standard": r["last_generation_standard"],
            "passed": (errs + warns) == 0,
            "error_count": errs,
            "warning_count": warns,
            "issue_types": dict(issue_types),
        })

    return {
        "scheme_id": scheme_id,
        "total": len(rows),
        "by_standard": dict(by_std),
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "total_issues": total_issues,
        "issue_type_counts": dict(issue_types),
        "sections": sections_out,
    }


@router.post("")
async def create_section(scheme_id: str, data: SectionCreate, db=Depends(get_db)):
    # ✅ BUG 修复（2026-09-23 · 守卫缺口）：与 update_section 同口径 —— 目录生成
    #    运行中新增章节，会在 AI 确认闸门落库时被整表重建覆盖，且新章参与
    #    renumber 会与生成中的编号重排竞态。补齐守卫（只拦 running/paused）。
    if outline_generation_in_progress(scheme_id):
        raise HTTPException(409, "本方案目录正在后台生成中，请等待生成完成（或先停止任务）后再新增章节")
    cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    sid = str(uuid.uuid4())
    # ✅ 双重编号防御缺口修复（2026-09-23）：PATCH(update_section) 路径早已剥离
    #    标题内嵌编号，唯独 POST 新增路径直写 data.title —— 客户端/复制粘贴带入
    #    "1.1 编制依据" 时，前端树与导出会再套一次位置编号 → "1.1 1.1 编制依据"。
    #    口径与 update_section 一致：先剥后存；剥离后为空时 strip_outline_numbering
    #    自动回退原标题（标题本身即编号时不丢内容）。
    if isinstance(data.title, str) and data.title.strip():
        data.title = strip_outline_numbering(data.title)
    level = data.level
    if data.parent_id:
        # ✅ BUG 修复（2026-09-29 · parent_id 零校验）：旧实现只查 level、
        #    不校验存在性与方案归属，直接把传入的 parent_id 原样入库。后果：
        #    客户端/脚本传入任意 UUID（或其它方案的 section_id）时，
        #    sections.parent_id 出现悬挂引用 —— _build_tree 把它当孤儿挂到根
        #    （用户看到章节「凭空移到顶层」），且跨方案篡改无任何拦截；
        #    而 update_section 早有「存在 + 同方案」强校验，两条路径口径分叉。
        #    现补齐同口径校验（创建时 sid 尚未生成，无「自引用」可能，故无环检测）。
        p = await db.execute(
            "SELECT id, level FROM sections WHERE id=? AND scheme_id=?",
            (data.parent_id, scheme_id))
        pr = await p.fetchone()
        if not pr:
            raise HTTPException(400, "父节点不存在或不属于本方案")
        level = pr["level"] + 1
        # ✅ BUG 修复（2026-09-29 · 深度上限分叉）：MAX_OUTLINE_DEPTH 是目录深度的
        #    唯一事实源，save-outline / normalize_outline 都按它裁剪，唯独手工新增
        #    章节路径无上限 —— 在三级章节下新增会落库四级，而前端目录树按三级
        #    渲染，该章节已入库却在界面上不可见（数据与展示分叉）。
        if level > MAX_OUTLINE_DEPTH:
            raise HTTPException(400, _DEPTH_EXCEEDED_MSG)
    sort_order = data.sort_order
    if sort_order == 0:
        # ✅ BUG 修复（2026-09-22）：SectionCreate.sort_order 默认 0，旧实现原样入库 ——
        # 新建章节永远排在同级最前（_build_tree 按 sort_order 升序）。
        # 现默认追加到同级末尾；显式传非零 sort_order 仍以传参为准（向后兼容）。
        cur = await db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM sections"
            " WHERE scheme_id=? AND COALESCE(parent_id,'')=?",
            (scheme_id, data.parent_id or ""))
        sort_order = (await cur.fetchone())[0]
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, description, level, word_budget, sort_order)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, scheme_id, row[0], data.parent_id, data.title, data.description, level,
         data.word_budget, sort_order))
    # ✅ BUG 修复（2026-09-22 · UUID 泄漏进提示词）：新建章节必须立即纳入编号命名空间。
    #    旧实现 outline_json 留空，_section_outline_number 回退返回 UUID 主键 ——
    #    正文生成提示词的「当前章节编号」直接被注入一串 UUID，
    #    模型照抄进正文子标题或整体层级编号规则错乱。
    #    （/reorder、save-outline、上传落库均写编号，唯独此路径漏写；
    #    统一复用 renumber_sections_after_reorder 回写，位置即编号唯一事实源。）
    await renumber_sections_after_reorder(db, scheme_id)
    # ✅ 编号统一（2026-09-26 · D4）：新增章节使后续章节编号顺移，按新编号统一
    #    重规范化所有含正文章节的子标题（落库正文与导出成稿同源，避免旧号滞留）。
    await _renormalize_all_section_contents(db, scheme_id)
    # ✅ 新增章节同样是结构变更（后续章节编号顺移）→ 作废扫描缓存
    await invalidate_consistency_scan_cache(db, scheme_id)
    await db.commit()
    return {"id": sid}


@router.patch("/{section_id}")
async def update_section(scheme_id: str, section_id: str, data: SectionUpdate, db=Depends(get_db)):
    # ✅ BUG 修复（2026-09-23 · 守卫缺口）：目录生成运行中手工编辑章节会被
    #    AI 确认闸门的整表重建静默覆盖（丢失更新）。save-outline / apply-and-save
    #    已有 outline_generation_in_progress 守卫，唯独此路径漏接 —— 现补齐，
    #    口径与 content_generation_in_progress 一致（只拦 running/paused）。
    if outline_generation_in_progress(scheme_id):
        raise HTTPException(409, "本方案目录正在后台生成中，请等待生成完成（或先停止任务）后再编辑章节")
    # ✅ 修复：校验章节归属该方案（防止跨方案篡改）
    cur = await db.execute("SELECT id FROM sections WHERE id=? AND scheme_id=?", (section_id, scheme_id))
    if not await cur.fetchone():
        raise HTTPException(404, "章节不存在")
    # 审核状态只能通过 review 专用入口修改，避免普通章节 PATCH 绕过状态机与留痕。
    if "review_status" in data.model_fields_set:
        raise HTTPException(422, "review_status 只能通过审核工作流接口修改")
    fields = {k: v for k, v in data.model_dump(exclude_none=True).items()}

    # ✅ BUG 修复（2026-09-22 · 双重编号）：改名 PATCH 必须剥离标题内嵌编号 ——
    #    save-outline / reorder 链路均经 strip_outline_numbering 清洗，
    #    唯独此路径直写原标题入库：用户改名为「1.1 编制依据」后，
    #    前端树与导出 DOCX 按编号规则再拼一次 → 「1.1 1.1 编制依据」双重编号。
    #    剥离后为空时回退原标题（与 strip_outline_numbering 既有行为一致）。
    if isinstance(fields.get("title"), str) and fields["title"].strip():
        fields["title"] = strip_outline_numbering(fields["title"])

    # ✅ 加固（2026-09-18）：parent_id 变更校验。旧实现无条件接受任意 parent_id，
    #    构造 A.parent=B / B.parent=A 后 `_build_tree` 会把二者互相挂为子节点 →
    #    两者都不出现在 roots，**整棵子树从目录树中消失**（前端表现为章节"凭空没了"），
    #    且正文生成读不到这些章节。现校验：父节点存在且同方案、不能自引用、不能成环。
    if "parent_id" in fields:
        new_parent = str(fields.get("parent_id") or "").strip()
        if new_parent:
            if new_parent == section_id:
                raise HTTPException(400, "章节不能以自身作为父节点")
            cur = await db.execute(
                "SELECT id FROM sections WHERE id=? AND scheme_id=?",
                (new_parent, scheme_id))
            if not await cur.fetchone():
                raise HTTPException(400, "父节点不存在或不属于本方案")
            # 环检测：新父节点不得是当前章节的后代
            descendants: set[str] = set()
            pending_ids = [section_id]
            while pending_ids:
                # ✅ 2026-09-30：补 scheme_id 限定。旧实现 `WHERE parent_id=?` 不带
                #    方案过滤，一旦库内存在跨方案 parent_id 脏数据（历史悬挂引用），
                #    遍历会越界进入其它方案 —— 既可能误判成环（把无关节点当后代
                #    拦下合法移动），也是跨方案读取面。父节点校验（上方）已限定同
                #    方案，此处应同口径。
                cur = await db.execute(
                    "SELECT id FROM sections WHERE parent_id=? AND scheme_id=?",
                    (pending_ids.pop(), scheme_id))
                for r in await cur.fetchall():
                    if r["id"] not in descendants:
                        descendants.add(r["id"])
                        pending_ids.append(r["id"])
            if new_parent in descendants:
                raise HTTPException(400, "不能把章节移动到它自己的子章节下（会形成循环引用）")

            # ✅ 2026-09-30 P0 修复：移动路径补深度上限校验。MAX_OUTLINE_DEPTH=3
            #    是目录深度的唯一事实源，create_section / save-outline / reorganize /
            #    _outline_skeleton 四条路径都按它裁剪或校验，唯独本路径没有 —— 把一级
            #    章节移动到三级章节下会静默成功，随后 renumber_sections_after_reorder
            #    把 sections.level 写成 4，而前端目录树按三级渲染：该章节已入库却在
            #    界面上彻底消失（用户看不到也无法恢复），是比丢失更难发现的
            #    数据/展示分叉。校验必须放在「写入 + commit + renumber」之前，否则
            #    结构变更事务已提交，无法回滚。
            cur = await db.execute(
                "SELECT level FROM sections WHERE id=? AND scheme_id=?",
                (new_parent, scheme_id))
            row = await cur.fetchone()
            if row and row["level"] + 1 > MAX_OUTLINE_DEPTH:
                raise HTTPException(400, _DEPTH_EXCEEDED_MSG)

    # ✅ G9：本次保存是否使审核结论失效（未传 content 时为 False）
    _review_reset = False
    # 正文子标题编号规范化待办标记：必须在**结构重排之后**执行（见下方说明）
    _content_renorm_pending = False
    if "content" in fields:
        # ✅ 竞态守卫：后台正文生成任务运行中，其 _persist_section 为无条件 UPDATE，
        # 用户此刻保存会与生成结果互相覆盖（丢失更新）。返回 409 提示等待任务完成。
        # ✅ G12-4：状态感知守卫（只拦 running/paused）—— 见
        # content_generation_in_progress 的说明，终态残留条目不再永久阻塞保存
        if content_generation_in_progress(scheme_id):
            raise HTTPException(409, "本方案正文正在后台生成中，手动保存会与生成结果互相覆盖，请等待生成完成后再编辑")
        # ✅ 口径与正文生成落库统一：只数正文文字，不含内嵌图表代码块
        fields["word_count"] = text_word_count(fields["content"])
        # ✅ BUG 修复：旧实现无条件把 status 置 generated（即使客户端没传），
        # 用户在 reviewed/expanded 章节做一次小修改保存就被打回 generated，
        # 审核状态丢失。新规则：
        #   · 客户端显式传 status → 以传参为准；
        #   · 否则仅当当前状态 ∈ {empty, failed, pending} 时推进为 generated；
        #     reviewed/expanded/locked 等人工状态原样保留。
        if "status" not in fields:
            cur = await db.execute("SELECT status FROM sections WHERE id=?", (section_id,))
            _srow = await cur.fetchone()
            _old_status = _srow[0] if _srow else "empty"
            if _old_status in ("empty", "failed", "pending"):
                fields["status"] = "generated"
        # ✅ 修复：本次同时提交新 word_budget 时用新预算计算 word_status（旧实现用 DB 旧值）
        if "word_budget" in fields:
            wb = fields["word_budget"] or 1500
        else:
            cur = await db.execute("SELECT word_budget FROM sections WHERE id=?", (section_id,))
            row = await cur.fetchone()
            wb = row[0] if row else 1500
            wb = wb or 1500
        fields["word_status"] = word_status_for(fields["word_count"], wb)
        # ✅ G9（2026-09-21）：手动改写正文 → 原审核结论已失效，退回「待审核」并留痕。
        # 此前全库只有 review 路由写 review_status：用户重写完已通过的章节后
        # 状态仍挂着 approved，导出预检的 review 类问题也全部漏报。
        cur = await db.execute("SELECT content, review_status FROM sections WHERE id=?", (section_id,))
        _old_row = await cur.fetchone()
        _old_content = (_old_row[0] or "") if _old_row else ""
        _review_reset = False
        if _old_content != (fields["content"] or ""):
            # 重置由 review 模块统一写库 + 落 review_records（单一写入口）
            _review_reset = await reset_review_on_content_change(
                db, scheme_id, section_id, actor="用户手动编辑")
        # ✅ BUG 修复（2026-09-23 · B31 手动保存与图表登记脱钩）：
        #    旧实现仅生成链路（_persist_section）登记内联图表，用户在编辑器
        #    手动保存含 ```mermaid / ```chart-json 的正文时 chart_predictions
        #    完全不动 —— 删掉的图残留僵尸登记（导出幽灵图），新增的图无登记
        #    （图表清单/预检看不到）。现复用 register_inline_charts 全量同步
        #    （先清后登，不 commit、与下方正文 UPDATE 同事务提交）。
        #    enforce_limits=False：手动保存是用户显式意图，不做程序级限额裁剪；
        #    校验失败的块仍会被修复或删除（宁缺勿滥，避免导出坏图）。
        #    失败降级：登记异常不阻断保存（退回旧行为，仅日志告警）。
        try:
            _chart_n, _fixed = await register_inline_charts(
                db, scheme_id, section_id, fields["content"],
                enforce_limits=False)
            if (_fixed or "") != (fields["content"] or ""):
                # 修复/裁剪改写了正文 → 字数口径同步重算
                fields["content"] = _fixed
                fields["word_count"] = text_word_count(_fixed)
                fields["word_status"] = word_status_for(
                    fields["word_count"],
                    fields.get("word_budget") or wb)
            if _chart_n:
                logger.info("章节 %s 手动保存：同步登记 %d 个内联图表",
                            section_id[:8], _chart_n)
        except Exception as e:
            logger.warning("章节 %s 手动保存图表同步失败（保留原文）: %s",
                           section_id[:8], e)
        # ✅ 统一编号命名空间（2026-09-26 · 补齐死代码）：正文落库前把 Markdown
        #    子标题编号规范化为导出口径（与 export.write_section 同一套算法），使
        #    「前端预览 = 落库正文 = 导出成稿」三处同源。实现收口到
        #    numbering.normalize_section_content_subheadings（唯一实现，内含
        #    content_subheading_renumber 开关 + 降级兜底），禁止再内联样板。
        #    本节存在 DB 子章节时降级为节内 body 命名空间（1）/ a、），
        #    彻底隔离 DB 子章节与正文子标题的编号冲突。
        _content_renorm_pending = True
    if fields:
        fields["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        await db.execute(f"UPDATE sections SET {sets} WHERE id=?", (*fields.values(), section_id))
        await db.commit()
    # ✅ BUG 修复（2026-09-22 · 层级迁移编号不同步）：结构字段（parent_id / level /
    #    sort_order）变更后必须整树重算编号。旧实现只挪了父指针、不刷新
    #    outline_json.id —— 移动章节后其自身与全部子孙的编号与实际位置漂移，
    #    正文提示词「当前章节编号」错号、导出目录树编号与前端显示不一致。
    #    统一收口到 renumber_sections_after_reorder（与 /reorder 同一口径：
    #    树中位置是编号的唯一事实源，显式传入的 level 也以位置折算为准）。
    _struct_changed = bool({"parent_id", "level", "sort_order"} & set(fields.keys()))
    if _struct_changed:
        await renumber_sections_after_reorder(db, scheme_id)
        # ✅ 编号统一（2026-09-26 · D4）：结构变更（移动/层级/排序）使本节及兄弟章节
        #    编号整体顺移，按新编号统一重规范化所有含正文章节的子标题（落库正文与导出
        #    成稿同源）。单章的规范化由下方 _content_renorm_pending 分支覆盖（无结构变更时）。
        await _renormalize_all_section_contents(db, scheme_id)
    # ✅ BUG 修复（2026-09-26 · 编号规范化时序）：正文子标题编号规范化必须在
    #    **结构重排之后**执行。旧实现把它放在 fields UPDATE 之前 —— 同一次请求里
    #    既改 parent_id 又改正文时（如「把第 3 章挂到第 1 章下并补充正文」），
    #    规范化读到的是**移动前**的 outline_json.id，随后 renumber_sections_after_reorder
    #    又把编号改成新位置 → 正文子标题永远停留在旧编号，与目录/导出永久错位。
    #    现移到重排之后，按最终编号规范化。
    #    ⚠️ 结构变更时该规范化已由 _renormalize_all_section_contents 统一处理（覆盖本节
    #    及所有兄弟章节），此处仅负责「纯改正文、无结构变更」的情况，避免重复写库。
    if _content_renorm_pending and not _struct_changed:
        _new_content, _changed = await normalize_section_content_subheadings(
            db, scheme_id, section_id, fields.get("content") or "")
        if _changed:
            _wb = fields.get("word_budget")
            if not _wb:
                _c = await db.execute(
                    "SELECT word_budget FROM sections WHERE id=?", (section_id,))
                _r = await _c.fetchone()
                _wb = (_r[0] if _r else 1500) or 1500
            fields["content"] = _new_content
            fields["word_count"] = text_word_count(_new_content)
            fields["word_status"] = word_status_for(fields["word_count"], _wb)
            fields["updated_at"] = datetime.now().isoformat()
            await db.execute(
                "UPDATE sections SET content=?, word_count=?, word_status=?,"
                " updated_at=? WHERE id=?",
                (_new_content, fields["word_count"], fields["word_status"],
                 fields["updated_at"], section_id))
    # ✅ 结构变更或改名 → 作废一致性扫描缓存（扫描行含章节定位/引用类冲突，
    #    正文指纹感知不到标题与编号变化；仅改正文时 content_hash 已自动失效，无需多清）
    #    ⚠️ 编号规范化虽改写正文，但改的仍是 content 字段本身 → content_hash 自动
    #    失效旧行即可。此处**不可**把 _content_renorm_pending 计入条件：那会让
    #    「只改正文且无子标题」的普通保存也整方案清空（过度失效，与本段意图相反）。
    if _struct_changed or "title" in fields:
        await invalidate_consistency_scan_cache(db, scheme_id)
    await db.commit()
    # ✅ G9/G10：前端据此提示「审核结论已失效」并刷新审核工作台
    # （此前 update 只回 {"ok": true}，连 word_count 都不带，
    #  前端 `data.word_count` 恒为 undefined → 成功提示里显示「已保存，当前 0 字」）
    return {"ok": True, "word_count": int(fields.get("word_count") or 0),
            "review_reset": _review_reset}


@router.delete("/{section_id}")
async def delete_section(scheme_id: str, section_id: str, db=Depends(get_db)):
    # ✅ BUG 修复（2026-09-23 · 守卫缺口）：目录生成运行中删章与 AI 确认闸门
    #    的整表重建直接写写竞态（删了又被重建回来，或闸门落库时章节已消失）。
    #    补齐守卫，与 save-outline 同口径。
    if outline_generation_in_progress(scheme_id):
        raise HTTPException(409, "本方案目录正在后台生成中，请等待生成完成（或先停止任务）后再删除章节")
    # ✅ 修复：校验章节归属该方案（防止跨方案删除）
    cur = await db.execute(
        "SELECT id FROM sections WHERE id=? AND scheme_id=?", (section_id, scheme_id))
    if not await cur.fetchone():
        raise HTTPException(404, "章节不存在")
    # 批量收集所有后代 ID 后一次性删除
    all_ids = [section_id]
    pending = [section_id]
    while pending:
        pid = pending.pop(0)
        # ✅ 2026-09-30：补 scheme_id 限定（P1-4）。旧实现不带方案过滤，库内若存在
        # 跨方案 parent_id 脏数据（历史悬挂引用 / 外部脚本直写 / 库复制后 id 碰撞），
        # 会把其它方案的章节一并收进删除集合 —— 即便 DELETE 语句本身带 scheme_id
        # 限定（章节行不受影响），越界 id 仍会流到下方 chart_predictions 清理
        # （该语句无方案限定）→ 误删其它方案章节的图表登记。与同文件
        # update_section 环检测同口径。
        cur = await db.execute(
            "SELECT id FROM sections WHERE parent_id=? AND scheme_id=?",
            (pid, scheme_id))
        children = [r[0] for r in await cur.fetchall()]
        all_ids.extend(children)
        pending.extend(children)
    placeholders = ",".join("?" * len(all_ids))
    await db.execute(
        f"DELETE FROM sections WHERE id IN ({placeholders}) AND scheme_id=?",
        all_ids + [scheme_id])
    # ✅ 修复：同步清理 chart_predictions 残留（否则导出 fallback 会挂上已删章节旧图）
    await db.execute(f"DELETE FROM chart_predictions WHERE section_id IN ({placeholders})", all_ids)
    # ✅ BUG 修复（2026-09-22 · 删除后编号断号）：旧实现删除后不重排剩余章节的
    #    outline_json.id —— 删掉 "2" 后目录编号变成 1/3/4 断号，前端树与
    #    正文提示词「当前章节编号」错乱，后续新增/拖拽才被动纠正。
    #    与 /reorder 同口径：删除即按新树位置统一重排（空树时自然无操作）。
    await renumber_sections_after_reorder(db, scheme_id)
    # ✅ 编号统一（2026-09-26 · D4）：删章后剩余章节编号顺移，按新编号统一重规范化
    #    所有含正文章节的子标题（落库正文与导出成稿同源，避免旧号滞留）。
    await _renormalize_all_section_contents(db, scheme_id)
    # ✅ 删章后作废扫描缓存：剩余章节编号顺移 + 被删章节的孤儿缓存行一并清理
    await invalidate_consistency_scan_cache(db, scheme_id)
    await db.commit()
    return {"ok": True}


@router.post("/reset-content")
async def reset_content(scheme_id: str, db=Depends(get_db)):
    """重置正文：清空目录树中**所有章节**已生成的正文（2026-09-20 新增）。

    - 清空范围：content / word_count / status / word_status / review_status
      （正文已不存在，章节审核状态随之失效回归初始）+ 随正文一并生成的
      内联图表列（flowchart_json 等 8 列）与 chart_predictions（对齐
      delete_section 的残留清理口径，防止导出 fallback 挂上旧图）；
    - **保留范围**：目录结构（title / parent_id / level / sort_order）、
      字数预算、locked 与 outline_json —— 只重置"正文"，不重置"目录"；
    - 竞态守卫：本方案正文生成任务运行中返回 409（与 update_section
      手动保存同一口径，避免生成结果落库覆盖刚重置的状态）。
    """
    cur = await db.execute("SELECT id FROM schemes WHERE id=?", (scheme_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "方案不存在")
    # ✅ 竞态守卫：后台生成任务运行中拒绝重置（否则清空后生成继续落库，
    #    用户看到"重置了但又有正文出现"的诡异现象）
    # ✅ G12-4：状态感知守卫（只拦 running/paused），与 update_section 同一入口
    if content_generation_in_progress(scheme_id):
        raise HTTPException(409, "本方案正文正在后台生成中，请等待生成完成（或先停止任务）后再重置")
    # 先统计本次将清除正文的章节数（UPDATE rowcount 统计的是命中行，
    # 重复重置时也会等于全量章节数，无法表达"清了几章"）
    cur = await db.execute(
        "SELECT COUNT(*) FROM sections WHERE scheme_id=? AND COALESCE(content,'')!=''",
        (scheme_id,))
    cleared = (await cur.fetchone())[0]
    await db.execute(
        "UPDATE sections SET content='', word_count=0, word_status='normal',"
        " status='empty', review_status='',"
        " flowchart_json='', gantt_json='', architecture_json='', labor_json='',"
        " comparison_json='', layout_json='', timeline_json='', inlined_chart_json='',"
        " updated_at=? WHERE scheme_id=?",
        (datetime.now().isoformat(), scheme_id))
    # 内联图表已清 → chart_predictions 一并清理（否则导出 fallback 仍会引用旧图）
    await db.execute("DELETE FROM chart_predictions WHERE scheme_id=?", (scheme_id,))
    await db.commit()
    logger.info("方案 %s 重置正文：清除 %d 个章节", scheme_id[:8], cleared)
    return {"ok": True, "cleared": cleared}


async def _save_outline_to_db(db, scheme_id: str, outline: list, source: str = "",
                             lock_roots: bool = False) -> dict:
    """核心保存逻辑：将目录树写入 sections 表（智能保留已有正文）。

    被 save_outline 路由和 outline_library.apply_and_save 共用。
    返回 {"ok": True, "count": n, "tree": [...]}。

    ✅ P5 修复（2026-09-23 · 数据流审计）：lock_roots 收进本函数、并入末尾
       同一次 commit。旧实现由 save_outline 端点在 _save_outline_to_db 提交
       之后另起第二个事务写 locked=1，两事务之间若失败，目录已落库但一级
       锁定丢失（用户以为已确认一级结构，实际未锁，下次 AI 生成可覆盖）。
       现整表重建 + 一级锁定在同一事务内原子提交，失败由连接池归还时统一回滚。
    """
    if not isinstance(outline, list):
        raise HTTPException(400, "outline 必须是数组")

    # ⚠️ 顺序要求：必须"先保存原始 id，再规范化"。
    # normalize_outline = 三级裁剪 + renumber_outline，后者会把节点 id
    # 覆盖成 "1"/"1.1" 编号。若先规范化，则 __original_id 记录的就不是
    # DB 主键而是编号，is_new 判定全部为 True → 已有章节被整表重建、
    # 已生成正文全部丢失。
    def _preserve_original_ids(nodes: list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            # ✅ BUG 修复（2026-09-26）：已有 __original_id 时**不得覆盖**。
            #    旧实现无条件写 `node["__original_id"] = node.get("id", "")`，
            #    会把 /adjust-outline 回传的真实 DB 主键（前端已透传）冲掉、
            #    退化成用「已被 renumber 覆写的展示编号」当主键 → 匹配不到任何
            #    已有 section → is_new 全为 True → 用户确认调整即整表重建，
            #    **已生成正文全部丢失**。仅在缺失时才用 id 兜底。
            if not node.get("__original_id"):
                node["__original_id"] = node.get("id", "")
            children = node.get("children")
            if isinstance(children, list) and children:
                _preserve_original_ids(children)

    _preserve_original_ids(outline)

    # 目录限三级 + 统一重排编号：任何来源落库前统一规范化
    from app.services.outline_utils import normalize_outline
    raw_count = len(outline)
    outline = normalize_outline(outline)
    # ✅ 健壮性修复：传入的是非空数组、但节点全非法（如 ["x", 1]）时，
    #    旧实现会得到空树并进入"空数组 = 清空全部目录"分支，
    #    把用户已有目录整表删掉。这里显式拒绝非法输入。
    if raw_count and not outline:
        raise HTTPException(400, "outline 节点格式非法（应为对象数组）")
    if not outline:
        # 空数组 = 清空全部目录
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "方案不存在")
        # ✅ 清空同样是「正文批量丢失」操作，量化后回传（与非空分支同口径）
        cur = await db.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id=?"
            " AND COALESCE(content,'')!=''", (scheme_id,))
        _cleared = (await cur.fetchone())[0]
        await db.execute("DELETE FROM sections WHERE scheme_id=?", (scheme_id,))
        await db.execute("DELETE FROM chart_predictions WHERE scheme_id=?", (scheme_id,))
        # ✅ BUG 修复（2026-09-21）：清空目录后 status 仍写成「目录已确认」。
        # 该值是编译状态语义（草稿 / 目录已确认 / 已完成），前端头部 Tag 与
        # review 端点的 scheme_status 都按此展示 —— 目录已清空还标「已确认」，
        # 会让用户与审核端同时误判「目录已就绪」，而实际方案里一个章节都没有。
        # 现回退到 OUTLINE_EMPTY_STATUS，并一并清空 outline_source
        # （来源已随目录一起删除，保留旧来源会造成溯源失真）。
        # 注：source 有值与否不影响目标状态，两分支合并（旧实现的分支差异
        # 只是「是否额外写 outline_source」，而这里两个方向都是清空）。
        await db.execute(
            "UPDATE schemes SET outline_source='', status=?, updated_at=? WHERE id=?",
            (OUTLINE_EMPTY_STATUS, datetime.now().isoformat(), scheme_id))
        # ✅ BUG 修复（2026-09-29 · 缓存未失效）：清空目录同样删掉了章节，
        #    非空分支末尾有 invalidate_consistency_scan_cache，唯独此分支漏调 ——
        #    被删章节的扫描行成为孤儿残留，下次一致性扫描/预检可能命中脏结果，
        #    把早已不存在的章节报成「仍有冲突」。与其它结构变更入口同口径补齐。
        await invalidate_consistency_scan_cache(db, scheme_id)
        await db.commit()
        _empty_result: dict = {"ok": True, "count": 0, "tree": []}
        if _cleared:
            _empty_result["cleared_content_sections"] = _cleared
            logger.warning("方案 %s 清空全部目录：%d 个章节的已生成正文被清除",
                           scheme_id[:8], _cleared)
        return _empty_result

    cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    project_id = row[0]

    # 加载现有 sections（建立 id -> dict 映射）
    cur = await db.execute(
        "SELECT id, parent_id, title, description, level, word_budget, outline_json, sort_order"
        " FROM sections WHERE scheme_id=?", (scheme_id,))
    existing_rows = [dict(r) for r in await cur.fetchall()]
    existing_by_id = {r["id"]: r for r in existing_rows}
    # ✅ 正文可丢性台账（2026-09-26）：整表重建前记录「哪些章节已有正文」。
    #    旧实现保存后只回 {ok,count,tree}，用户点「确认并保存目录」把 AI 新目录
    #    落库时，未匹配上的旧章节被级联 DELETE → 正文静默消失、无任何提示
    #    （AI 生成链路回传的节点 id 是展示编号 "1"/"1.1"，必然匹配不到 DB 主键）。
    #    现于返回值与日志中如实回传被清除正文的章节数，前端据此明示。
    content_ids: set[str] = set()
    try:
        cur = await db.execute(
            "SELECT id FROM sections WHERE scheme_id=? AND COALESCE(content,'')!=''",
            (scheme_id,))
        content_ids = {r[0] for r in await cur.fetchall()}
    except Exception as e:  # noqa: BLE001 — 纯统计，失败不影响保存
        logger.warning("统计已有正文章节失败（不影响保存）: %s", e)

    final_section_ids: set[str] = set()
    node_counter = {"n": 0}
    new_inserts: list[tuple] = []
    updates: list[tuple] = []

    async def _process_nodes(nodes: list, parent_db_id: str):
        for sort_order, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            original_id = node.get("__original_id", "")
            numbered_id = node.get("id", "")
            title = node.get("title", "")
            description = node.get("description", "")
            level = node.get("level", 1)
            confidence = node.get("confidence")
            if confidence is None:
                confidence = 1.0
            # ✅ 统一形态（2026-09-22）：outline_json 固定写 id/level/confidence 三键。
            #    旧实现缺 level 键，与 renumber_sections_after_reorder / 上传落库的
            #    写入口径不一致（三处写入形态各异，消费方需逐处容错）。
            outline_json = json.dumps(
                {"id": numbered_id, "level": level, "confidence": confidence},
                ensure_ascii=False
            )
            is_new = (
                not original_id
                or original_id.startswith("local_")
                or original_id not in existing_by_id
            )
            # ✅ BUG 修复：请求未携带 word_budget 时保留库中已有预算。
            #    旧实现无条件回落 1500——"只改标题/结构调整"的保存会把用户
            #    已设置的字数预算全部重置（AI/上传来源的节点本就不含该字段）。
            raw_budget = node.get("word_budget")
            existing = None if is_new else existing_by_id.get(original_id)
            if isinstance(raw_budget, (int, float)) and raw_budget > 0:
                word_budget = int(raw_budget)
            elif existing and existing.get("word_budget"):
                word_budget = existing["word_budget"]
            else:
                word_budget = 1500
            if not is_new:
                db_id = original_id
                final_section_ids.add(db_id)
                updates.append((title, description, level, parent_db_id,
                                word_budget, outline_json, sort_order, db_id))
            else:
                db_id = str(uuid.uuid4())
                final_section_ids.add(db_id)
                new_inserts.append((
                    db_id, scheme_id, project_id, parent_db_id,
                    title, description, level, sort_order,
                    "empty", outline_json, word_budget,
                ))
            node_counter["n"] += 1
            children = node.get("children")
            if isinstance(children, list) and children:
                await _process_nodes(children, db_id)

    await _process_nodes(outline, "")

    if updates:
        await db.executemany(
            "UPDATE sections SET title=?, description=?, level=?, parent_id=?,"
            " word_budget=?, outline_json=?, sort_order=?, updated_at=? WHERE id=?",
            [(*u[:-1], datetime.now().isoformat(), u[-1]) for u in updates])

    if new_inserts:
        await db.executemany(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " description, level, sort_order, status, outline_json, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            new_inserts)

    old_ids = {r["id"] for r in existing_rows}
    to_delete = old_ids - final_section_ids
    if to_delete:
        children_map: dict[str, list[str]] = {}
        for r in existing_rows:
            pid = r.get("parent_id", "")
            children_map.setdefault(pid, []).append(r["id"])
        def _collect_delete_ids(ids: set) -> set:
            """级联收集后代 ID，但**跳过仍在本次目录中的章节**。

            ✅ BUG 修复：旧实现不加 final_section_ids 过滤，当用户把子章节
            C 从父章节 P 下拖到别处、同时删除 P 时，to_delete={P}，
            children_map[P]=[C] → C 被一并加入删除集合。
            而 C 刚刚已按新结构 UPDATE 过（final_section_ids 含 C），
            结果是"更新后立刻被删"，正文与预算全部丢失。
            """
            all_ids = set(ids)
            changed = True
            while changed:
                changed = False
                for nid in list(all_ids):
                    for cid in children_map.get(nid, []):
                        if cid in final_section_ids:
                            continue
                        if cid not in all_ids:
                            all_ids.add(cid)
                            changed = True
            return all_ids
        full_delete_ids = _collect_delete_ids(to_delete)
        placeholders = ",".join("?" * len(full_delete_ids))
        await db.execute(
            f"DELETE FROM sections WHERE id IN ({placeholders})",
            list(full_delete_ids))
        await db.execute(
            f"DELETE FROM chart_predictions WHERE section_id IN ({placeholders})",
            list(full_delete_ids))

    if source:
        await db.execute("UPDATE schemes SET outline_source=?, status=?, updated_at=? WHERE id=?",
                         (source, OUTLINE_SAVED_STATUS, datetime.now().isoformat(), scheme_id))
    else:
        await db.execute("UPDATE schemes SET status=?, updated_at=? WHERE id=?",
                         (OUTLINE_SAVED_STATUS, datetime.now().isoformat(), scheme_id))
    # ✅ P5 修复（2026-09-23）：一级目录锁定与整表重建同事务提交（见函数 docstring）。
    #    仅在 lock_roots=true 且本次确有节点落库时置 locked=1。
    roots_locked = False
    if lock_roots and node_counter["n"]:
        await db.execute(
            "UPDATE sections SET locked=1 WHERE scheme_id=? AND level=1", (scheme_id,))
        roots_locked = True
    # ✅ 编号统一（2026-09-26 · D4 口径补齐）：整表重建会改变保留章节的编号
    #    （在中间插入一章 → 后续章节整体顺移）。create / delete / reorder / PATCH
    #    四个入口都调了 _renormalize_all_section_contents，唯独 save-outline 与
    #    目录库套用（apply-and-save 复用本函数）没有 —— 落库正文的子标题编号因此
    #    停留在旧号，只有导出时才重算，导致「前端预览 ≠ 落库正文」且
    #    /numbering-consistency 长期报漂移。此处补齐，同事务提交。
    await _renormalize_all_section_contents(db, scheme_id)
    # ✅ 整表重建（save-outline / 目录库套用共用此入口）→ 作废扫描缓存
    await invalidate_consistency_scan_cache(db, scheme_id)
    await db.commit()
    # ✅ 正文丢失量化（见上方 content_ids 说明）：以提交后的实际留存内容为准，
    #    不用「预期集合差集」估算 —— 级联删除/新增/复用同一条形都会影响结果。
    cleared_content = 0
    if content_ids:
        try:
            cur = await db.execute(
                "SELECT id FROM sections WHERE scheme_id=?"
                " AND COALESCE(content,'')!=''", (scheme_id,))
            kept = {r[0] for r in await cur.fetchall()}
            cleared_content = len(content_ids - kept)
        except Exception as e:  # noqa: BLE001
            logger.warning("统计被清除正文的章节数失败（忽略）: %s", e)
    tree = await _build_tree(db, scheme_id)
    def _strip_internal_fields(nodes: list):
        for n in nodes:
            n.pop("__original_id", None)
            if n.get("children"):
                _strip_internal_fields(n["children"])
    _strip_internal_fields(tree)
    result: dict = {"ok": True, "count": node_counter["n"], "tree": tree}
    if roots_locked:
        result["roots_locked"] = True
    if cleared_content:
        # 前端据此明示「本次保存清除了 N 个章节的已生成正文」，不再静默丢失
        result["cleared_content_sections"] = cleared_content
        logger.warning(
            "方案 %s 目录整表重建：未匹配到旧章节，%d 个章节的已生成正文被清除",
            scheme_id[:8], cleared_content)
    return result


@router.post("/save-outline")
async def save_outline(scheme_id: str, body: dict, db=Depends(get_db)):
    """保存目录树（AI 生成 / 上传识别 / 手动编辑后）

    智能保留已有章节的正文内容：
    - 前端传来的节点如果 id 匹配已有 section（非 local_ 开头），则只更新结构字段
    - 新增节点（local_xxx）创建新 section，默认 status='empty'
    - 不再存在的旧 section 被删除（级联删除其子节点）

    ✅ 竞态守卫（2026-09-21 补齐）：本端点会**整表重建 sections**（INSERT + UPDATE
    + 级联 DELETE），与后台正文生成的 _persist_section 无条件 UPDATE 直接冲突，
    也与后台目录生成的「一级目录确认闸门」互相覆盖。旧实现无任何守卫，
    两个入口同时写同一张表时丢失更新的后果不可恢复（正文被删、目录被覆盖）。
    """
    # ✅ 正文生成在跑 → 拒绝：正在写的章节可能被本端点的级联 DELETE 删掉，
    #    之后 _persist_section 的 UPDATE 命中 0 行，整章正文静默丢失。
    if content_generation_in_progress(scheme_id):
        raise HTTPException(
            409, "本方案正文正在后台生成中，请等待生成完成（或先停止任务）后再保存目录——"
                 "整表重建会与生成结果互相覆盖")
    # ✅ 目录生成在跑 → 拒绝：AI 结果完成后会弹出确认闸门，用户确认即整表覆盖
    #    刚手工保存的目录；反向亦然。前端已用 disabled 挡主路径，此处兜底
    #    直连调用 / 多标签页并发 / 断线重挂后旧标签页。
    if outline_generation_in_progress(scheme_id):
        raise HTTPException(
            409, "本方案目录正在后台生成中，请等待生成完成（或先停止任务）后再保存目录")

    outline = body.get("outline", [])
    source = body.get("source", "")
    # ✅ 一级目录确认闸门（对齐 OpenBidKit outline-selection）：
    #    lock_roots=true 时，保存后把全部一级章节置 locked=1（用户已确认一级结构）。
    #    ✅ P5 修复：锁定逻辑已下沉到 _save_outline_to_db，与整表重建同一事务提交，
    #    此处不再另起第二次 commit。
    lock_roots = bool(body.get("lock_roots"))
    result = await _save_outline_to_db(db, scheme_id, outline, source, lock_roots=lock_roots)
    return result


# ---------------------------------------------------------------------------
# 目录 AI 自然语言调整（引入自参考软件 outlineAdjustmentTask 的轻量版，2026-09-22）
# ---------------------------------------------------------------------------

def _collect_section_ids(nodes: list) -> set:
    """递归收集现有目录树的全部 DB section id（用于校验 AI 回传的 id 是否真实）。"""
    ids: set = set()
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        if nid:
            ids.add(str(nid))
        ids |= _collect_section_ids(n.get("children") or [])
    return ids


def _slim_tree_for_adjust(nodes: list) -> list:
    """把 DB 目录树裁剪为喂给 AI 的精简结构：只保留 id/title/description/children。

    ✅ 不把 status/word_count 等运营字段塞进 prompt（同等信息量下 token 浪费数倍，
    且会让模型误改无关字段）。id 保留为 DB 主键，供 AI 原样回传以保住正文关联。
    """
    slim: list = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        node = {
            "id": str(n.get("id") or ""),
            "title": str(n.get("title") or ""),
        }
        desc = str(n.get("description") or "").strip()
        if desc:
            node["description"] = desc
        children = _slim_tree_for_adjust(n.get("children") or [])
        if children:
            node["children"] = children
        slim.append(node)
    return slim


def _relink_adjusted_original_ids(nodes: list, valid_ids: set) -> None:
    """把 AI 回传的合法 DB id 落到 __original_id（保住正文），剔除编造的 id。

    ✅ normalize_outline → renumber_outline 会用「1、1.1」编号覆盖节点 id，
    故必须在规范化前把真实 DB id 转存到 __original_id（save-outline 依此匹配
    保留正文）。AI 若幻觉出一个不存在的 id，一律按新增节点处理（清除该 id），
    避免误挂到别的章节正文上。
    """
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        raw_id = str(n.get("id") or "").strip()
        if raw_id and raw_id in valid_ids:
            n["__original_id"] = raw_id
        else:
            # 编造/缺失 id：清空，交由 normalize 重排、save-outline 当作新增
            n.pop("id", None)
        _relink_adjusted_original_ids(n.get("children") or [], valid_ids)


def _adjust_outline_validate_fn(obj) -> list:
    """目录调整响应校验：outline 必须是非空、每项含 title 的节点数组。

    与生成链路同口径：深度超限交由 normalize_outline 裁剪兜底，此处不卡层级。
    """
    ol = obj.get("outline") if isinstance(obj, dict) else None
    if not isinstance(ol, list) or not ol:
        return ["outline 必须为非空数组"]
    bad = [n for n in ol
           if not isinstance(n, dict) or not str(n.get("title") or "").strip()]
    return [f"存在 {len(bad)} 个缺标题的非法节点"] if bad else []


@router.post("/adjust-outline")
async def adjust_outline(scheme_id: str, body: dict, db=Depends(get_db)):
    """目录 AI 自然语言调整：按用户要求定向修改现有目录，返回**待确认**新树。

    【引入背景】参考软件支持对已有目录提自然语言要求（如"把第3章拆成两章"、
    "删除应急预案章"）由 AI 定向修改；本软件此前只能手动编辑树或整表重新生成，
    缺少"改一小处却要走整轮生成"的轻量入口。现补齐该能力。

    【设计取舍 · 保持兼容 + 零数据风险】
    - 本端点**不落库**：仅返回 AI 按调整要求产出的新目录树 + 变更总结；用户确认
      后仍走既有 save-outline 落库，智能保留正文 / 级联删除逻辑完全复用不变。
    - 来自现有目录的节点回传 __original_id 保住正文关联，新增节点不带该键。
    - 未引入任何图表/人工配图相关逻辑（严守全自动图表约束）。
    - 一次性 collect_json_response 调用（复用熔断/配额冷却/并发保护），不引入
      参考软件的持久 Agent 会话基建（本软件无此设施，照搬成本/风险过高）。
    """
    instruction = str(body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "需要提供 instruction（调整要求）")

    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    scheme = await cur.fetchone()
    if not scheme:
        raise HTTPException(404, "方案不存在")
    scheme = dict(scheme)

    tree = await _build_tree(db, scheme_id)
    if not tree:
        raise HTTPException(400, "当前方案还没有目录，请先生成目录后再调整")

    valid_ids = _collect_section_ids(tree)
    slim = _slim_tree_for_adjust(tree)
    current_outline_text = json.dumps(slim, ensure_ascii=False)
    # 目录过大时截断保护 prompt 体积（超长方案兜底；正常三级目录远小于此）
    prompt = render(
        "outline_adjust_system",
        scheme_name=str(scheme.get("name") or ""),
        scheme_type=str(scheme.get("type") or ""),
        current_outline=current_outline_text[:30000],
        instruction=instruction[:3000],
    )
    try:
        obj, _ = await asyncio.wait_for(
            collect_json_response(
                [{"role": "system", "content": prompt}],
                _adjust_outline_validate_fn,
                json_mode=True, temperature=0.2, scene="outline_adjust"),
            timeout=settings.ai_adjust_timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, "目录调整超时，请缩小调整范围后重试")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("目录 AI 调整失败（scheme=%s）: %s", scheme_id, e)
        raise HTTPException(502, f"目录调整失败：{e}")

    new_outline = obj.get("outline") or []
    _relink_adjusted_original_ids(new_outline, valid_ids)
    normalized = normalize_outline(new_outline)
    return {
        "ok": True,
        "outline": normalized,
        "summary": str(obj.get("summary") or "").strip(),
    }


async def renumber_sections_after_reorder(db, scheme_id: str) -> None:
    """按当前 sort_order 顺序重排所有章节的 outline_json.id / level（章节编号）。

    拖拽排序只改 sort_order，但前端树与正文生成提示词都依赖 outline_json.id
    作为「章节编号」。本函数统一回写，避免拖拽后编号错乱。

    ✅ 编号统一（2026-09-25）：重排核心算法收敛到 services/numbering.py 唯一
    事实源（renumber_section_outline_ids），本函数只保留「建树 → 批量 UPDATE」
    的 DB 职责，避免与目录树/导出侧的编号算法漂移。
    """
    tree = await _build_tree(db, scheme_id)
    oj_updates = renumber_section_outline_ids(tree)
    if oj_updates:
        await db.executemany(
            "UPDATE sections SET outline_json=?, level=? WHERE id=? AND scheme_id=?",
            [(*u, scheme_id) for u in oj_updates])


async def _renormalize_all_section_contents(db, scheme_id: str) -> None:
    """结构变更（增/删/重排/移动）后，按重排后的新编号统一重规范化所有含正文章节
    的子标题编号，保证「落库正文 = 导出成稿」同源（修复 D4：重排后仅回写 outline_json，
    已落库正文的子标题仍用旧号，仅导出时重算，导致预览/校验失真）。

    - 复用 services.numbering.normalize_section_content_subheadings（唯一实现，含
      content_subheading_renumber 开关 + 降级兜底），逐章按新 outline_json 重写子标题；
    - ✅ 性能（2026-09-27 · 消除 N+1）：旧实现逐章调用规范化，而后者每次要查两次库
      （读 outline_json/level/title + COUNT 子章节）。200 章方案 = 401 次 execute，
      且该函数在 **5 个结构变更入口**（create/update/delete/reorder/save-outline）都会
      跑一次，拖拽排序这类高频操作实测卡顿数百毫秒。现复用编号模块已有的
      `load_scheme_section_index`（2 条查询压成一次预取，与
      validate_scheme_numbering_consistency 同一套优化），整轮降为 3 次查询；
      预取失败时传 index=None 回退逐章查询，**行为与旧版完全一致**（fail-soft）；
    - 单章失败仅告警、绝不阻断事务（编号规范化代价远低于正文丢失）；
    - 在 renumber 之后、commit 之前调用，复用同一事务。
    """
    try:
        from app.config import settings
        if not settings.content_subheading_renumber:
            return
    except Exception:
        pass
    try:
        from app.services.numbering import (
            load_scheme_section_index, normalize_section_content_subheadings)
        cur = await db.execute(
            "SELECT id, content FROM sections "
            "WHERE scheme_id=? AND COALESCE(content,'')!=''",
            (scheme_id,))
        rows = await cur.fetchall()
        # ✅ 编号元数据一次性预取（必须在 renumber 之后读，与本函数调用时序一致）
        index = await load_scheme_section_index(db, scheme_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("结构重排后批量重规范化：读取章节失败（跳过）: %s", e)
        return
    # ✅ 性能（2026-09-27 · 写侧同样批量）：逐章 `normalize_...` 的**读**已被预取消掉，
    #    但**写**仍是每章一条 UPDATE → 200 章 = 200 次 execute（拖拽排序实测卡顿主因）。
    #    现收集后 executemany 一次写完；executemany 失败则回退逐条写（保 fail-soft）。
    updates: list[tuple] = []
    for r in rows:
        sec_id = r["id"]
        content = r["content"] or ""
        if not content.strip():
            continue
        try:
            new_content, changed = await normalize_section_content_subheadings(
                db, scheme_id, sec_id, content, index=index)
            if changed:
                updates.append(
                    (new_content, text_word_count(new_content), sec_id, scheme_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("章节 %s 结构重排后子标题重规范化失败（保留原正文）: %s",
                           sec_id[:8], e)
    if not updates:
        return
    _SQL = ("UPDATE sections SET content=?, word_count=? "
            "WHERE id=? AND scheme_id=?")
    try:
        await db.executemany(_SQL, updates)
    except Exception as e:  # noqa: BLE001 — 批量失败必须回退逐条，不丢已算好的改写
        logger.warning("结构重排后批量重规范化：批量写库失败，回退逐条（%s）", e)
        for upd in updates:
            try:
                await db.execute(_SQL, upd)
            except Exception as e2:  # noqa: BLE001
                logger.warning("章节 %s 重规范化写库失败（保留原正文）: %s",
                               str(upd[2])[:8], e2)


@router.get("/numbering-consistency")
async def get_numbering_consistency(scheme_id: str, db=Depends(get_db)):
    """显式跨校验器：返回方案内所有含正文章节的编号一致性报告。

    用于「生成后校验目录与正文编号一致 / 导出前校验正文与导出编号一致」的手动或
    CI 触发入口；落库正文子标题编号与当前 outline 不一致（D4 类漂移）的章节会被列出。
    """
    return await validate_scheme_numbering_consistency(db, scheme_id)


@router.post("/numbering-consistency/repair")
async def repair_numbering_consistency(scheme_id: str, db=Depends(get_db)):
    """显式跨校验器：将落库正文子标题编号按当前 outline 重新规范化（修复 D4 类漂移）。

    ✅ 编号版本管理：修复前自动建 numbering_repair 快照，返回值含 snapshot_id，
    可经 POST /numbering-consistency/rollback/{snapshot_id} 一键回滚。
    """
    return await repair_scheme_numbering_consistency(db, scheme_id)


@router.get("/numbering-consistency/versions")
async def list_numbering_versions(scheme_id: str, limit: int = 20,
                                  db=Depends(get_db)):
    """编号版本历史：列出 numbering_repair / numbering_rollback 快照（新→旧）。"""
    return await _list_numbering_versions(db, scheme_id, limit)


@router.post("/numbering-consistency/rollback/{snapshot_id}")
async def rollback_numbering_version(scheme_id: str, snapshot_id: str,
                                     db=Depends(get_db)):
    """编号版本一键回滚：恢复指定快照涉及章节的修复前正文（回滚本身可撤销）。

    安全约束：快照必须存在（404）、属于当前方案（400）、type 为 numbering_*
    （400）——一致性修复快照走 /consistency/rollback，互不串用。
    """
    try:
        return await _rollback_numbering_version(db, scheme_id, snapshot_id)
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(404, msg)
        raise HTTPException(400, msg)


@router.post("/reorder")
async def reorder_sections(scheme_id: str, body: dict, db=Depends(get_db)):
    """拖拽排序后统一重排 sort_order 与编号"""
    # ✅ BUG 修复（2026-09-23 · 守卫缺口）：目录生成运行中拖拽改 sort_order，
    #    与 AI 确认闸门的整表重建 + 编号重排交叉执行会互相覆盖（编号双轨漂移）。
    #    补齐守卫，与 create/delete/update/save-outline 四处收口一致。
    if outline_generation_in_progress(scheme_id):
        raise HTTPException(409, "本方案目录正在后台生成中，请等待生成完成（或先停止任务）后再调整顺序")
    order = body.get("order", [])
    # ✅ 防御（2026-09-20）：旧实现对 body 类型零校验 —— order 传字符串/字典时
    #    enumerate 会逐字符/逐键值写入 sort_order（脏数据），非字符串元素则直接
    #    500。非法输入统一 400，并过滤空 id（executemany 对不存在 id 静默跳过）。
    if not isinstance(order, list):
        raise HTTPException(400, "order 必须是章节 id 数组")
    order = [sid for sid in order if isinstance(sid, str) and sid]
    # ✅ 性能优化：executemany 批量更新，替代逐条 await
    await db.executemany(
        "UPDATE sections SET sort_order=? WHERE id=? AND scheme_id=?",
        [(i, sid, scheme_id) for i, sid in enumerate(order)])

    # ✅ BUG 修复：旧实现只更新 sort_order，未重算 outline_json.id（章节编号）。
    #    前端树按 sort_order 重新计算编号展示，但后端 sections.outline_json.id
    #    仍是旧编号；正文生成提示词经 _section_outline_number 读取该 id 作为
    #    「当前章节编号」，导致拖拽后章节编号错乱（原第 3 章被当成第 1 章），
    #    正文里套错章节号。现拖拽后按新顺序统一重排编号并回写 outline_json.id / level。
    await renumber_sections_after_reorder(db, scheme_id)
    # ✅ 编号统一（2026-09-26 · D4）：拖拽重排后按新编号统一重规范化所有含正文章节
    #    的子标题（落库正文与导出成稿同源，避免重排后旧号滞留）。
    await _renormalize_all_section_contents(db, scheme_id)
    # ✅ 拖拽后作废扫描缓存（缓存键不含结构指纹，旧扫描行仍会命中）
    await invalidate_consistency_scan_cache(db, scheme_id)
    await db.commit()
    tree = await _build_tree(db, scheme_id)
    return {"ok": True, "tree": tree}


@router.get("/export-tree")
async def export_tree(scheme_id: str, db=Depends(get_db)):
    """导出目录树 JSON（程序重排编号）"""
    tree = await _build_tree(db, scheme_id)
    def to_outline(nodes):
        out = []
        for n in nodes:
            out.append({"id": n["id"], "title": n["title"], "description": n["description"],
                        "level": n["level"], "children": to_outline(n["children"])})
        return renumber_outline(out)
    return {"outline": to_outline(tree)}


# ---------- 字数压缩（对齐 OpenBidKit buildWordAdjustmentMessages） ----------
@router.post("/{section_id}/shrink")
async def shrink_section(scheme_id: str, section_id: str, db=Depends(get_db)):
    """压缩超字数章节正文（只缩不扩的补齐项）。

    本软件正文链路此前"只扩不缩"：超字数仅标记 word_status='over'，无缩减手段。
    本端点对齐 OpenBidKit 的字数调整修复器：
    - AI 只输出 replace/delete 局部操作（严禁整篇重写），程序级校验
      （target_text 逐字唯一 + 保护区间：代码块/表格/图片不可触碰）后应用；
    - 多轮封顶 SHRINK_MAX_ROUNDS，无进展提前退出；
    - 每轮结束重算字数，收敛到目标 1.15 倍以内即停。
    """
    cur = await db.execute(
        "SELECT id, title, content, word_budget FROM sections WHERE id=? AND scheme_id=?",
        (section_id, scheme_id))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "章节不存在")
    content = row["content"] or ""
    word_budget = row["word_budget"] or 1500
    cur_scheme = await db.execute(
        "SELECT name, type FROM schemes WHERE id=?", (scheme_id,))
    srow = await cur_scheme.fetchone()
    scheme_name = (srow["name"] if srow else "") or ""
    scheme_type = (srow["type"] if srow else "") or ""

    current = content
    before_wc = text_word_count(current)
    if before_wc == 0:
        raise HTTPException(400, "章节正文为空，无需压缩")
    # ✅ 阈值口径与 word_status 一致：超过预算 130%（over）才需压缩。
    # 旧实现 >预算即拒，1.1~1.3 倍的 normal 章节无法点压缩。
    if before_wc <= word_budget * WORD_OVER_RATIO:
        raise HTTPException(
            400, f"当前字数 {before_wc} 未超出目标 {word_budget} 的 130%（超字数状态才需压缩）")

    rounds_used = 0
    stop_reason = "已达轮次上限"

    def _prompt_factory(round_no: int, cur_wc: int) -> str:
        """渲染压缩提示词：占位符被改坏时降级为空上下文的同一条目重试一次。"""
        try:
            return render(
                "content_shrink_system",
                scheme_name=scheme_name, scheme_type=scheme_type,
                section_title=row["title"] or "",
                current_words=cur_wc, target_words=word_budget,
                round_no=round_no, max_rounds=SHRINK_MAX_ROUNDS)
        except Exception as e:
            # 提示词被用户改坏（占位符缺失等）不应中断压缩
            logger.warning("content_shrink_system 渲染失败: %s", e)
            return render("content_shrink_system",
                          scheme_name="", scheme_type="", section_title="",
                          current_words=cur_wc, target_words=word_budget,
                          round_no=round_no, max_rounds=SHRINK_MAX_ROUNDS)

    async def _call_ai(sys_prompt: str, user_payload: str) -> str:
        return await asyncio.wait_for(
            chat_with_fallback(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_payload}],
                timeout=180, scene="content_shrink"),
            timeout=240)

    # ✅ 多轮压缩主循环已抽出到 services/content_shrink.shrink_content_rounds：
    #    与正文生成链路的「超字数自动压缩」共用同一实现（避免 50 行轮次逻辑双份漂移）。
    shrink = await shrink_content_rounds(
        content, word_budget=word_budget,
        prompt_factory=_prompt_factory, call_ai=_call_ai)
    rounds_used = shrink["rounds_used"]
    stop_reason = shrink["stop_reason"]
    current = shrink["content"]

    if not shrink["applied"]:
        raise HTTPException(422, f"压缩未生效：{stop_reason}")

    final_wc = text_word_count(current)
    await db.execute(
        "UPDATE sections SET content=?, word_count=?, word_status=?, updated_at=? WHERE id=?",
        (current, final_wc, word_status_for(final_wc, word_budget),
         datetime.now().isoformat(), section_id))
    # ✅ G9（2026-09-21）：压缩改写了正文 → 原审核结论失效，退回「待审核」并留痕
    await reset_review_on_content_change(db, scheme_id, section_id, actor="字数压缩")
    await db.commit()
    logger.info("章节 %s 压缩完成：%d → %d 字（%d 轮，%s）",
                section_id[:8], before_wc, final_wc, rounds_used, stop_reason)
    return {
        "content": current,
        "word_count": final_wc,
        "word_status": word_status_for(final_wc, word_budget),
        "before": before_wc,
        "rounds_used": rounds_used,
        "stop_reason": stop_reason,
    }