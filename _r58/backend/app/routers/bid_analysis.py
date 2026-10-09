"""项目资料结构化提取路由（18 项 AI 并发提取：12 项方案基本信息 + 6 项施工组织设计）。

对齐《专项方案生成与编制 — 基本信息需求清单》，复用本项目 AI 基础设施：
- chat_with_fallback：带熔断降级的统一模型网关
- task_registry：长任务进度推送与暂停/恢复/停止
- SSE 心跳包装：10s 心跳防超时

完整流程：
  1. 归一化配置（mode=key/full/custom，必选项强制包含）
  2. 读取已解析的项目资料文本（project_documents 中 parsed_markdown）
  3. 计算分段（split_for_analysis）
  4. 先单独跑 projectBasicInfo（提示词缓存预热）
  5. 等 5 秒后并发跑其余项（每项独立 try/catch）
  6. 每项完成 → checkpoint（写 bid_analysis_items + 进度）
  7. 结束判定（必选项全部 success → 整体 success，否则 error）
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import settings
from app.db import get_db, safe_rowcount, settle_global_conn
from app.routers.schemes import require_project_id  # ✅ G3 根因加固（R45）
from app.services import scheme_classification as sc
from app.services.ai.provider_factory import chat_with_fallback
from app.services.ai.sse_utils import with_heartbeat
from app.services.ai.task_registry import (
    finish_task,
    has_active_task,
    is_stopped,
    register_task,
    update_progress,
    wait_resume,
)
from app.services.bid_analysis_service import (
    ANALYSIS_ITEMS,
    DEFAULT_CHUNK_SIZE,
    # ✅ 2026-09-30（第十一轮 · 招标响应域 + 断点续跑）：域注册表与主键唯一出口
    EXTRACTION_DOMAINS,
    MARKDOWN_MISSING_RESULT,
    REQUIRED_ITEM_IDS,
    AnalysisConfig,
    build_evidence_json,
    build_item,
    build_item_pk,
    build_system_messages,
    get_all_items,
    get_groups,
    get_groups_by_domain,
    get_item_def,
    get_item_domain,
    get_item_fields,
    get_item_prompt,
    get_items_by_domain,
    is_missing_result,
    split_for_analysis,
)

# ✅ 2026-09-22 新增（对齐 OpenBidKit bidSectionContext.cjs）：
#    多标段项目「当前投标范围」上下文注入 + 下游缓存失效。
from app.services.bid_section_context import (
    build_bid_section_context_hint,
    load_bid_section_row,
    resolve_section_hint,
    selected_section_from_row,
)
from app.services.bid_section_detector import detect_bid_sections

# ✅ 2026-09-25：文档分类唯一事实源（提取合并优先级口径与 global_facts 对齐）
from app.services.doc_categories import extract_priority
from app.services.facts_extractor import invalidate_export_cache

logger = logging.getLogger("bid_analysis")
router = APIRouter(prefix="/api/v1/bid-analysis", tags=["bid_analysis"])


def _safe_evidence(content: str, output_type: str, tender_text: str) -> str:
    """来源位置反查（不阻断业务的包装）：匹配失败/异常只记告警并返回空串。

    evidence 是锦上添花的溯源信息，绝不允许它的失败把一次成功的提取变成失败。
    """
    if not content:
        return ""
    try:
        return build_evidence_json(content, output_type, tender_text)
    except Exception as e:
        logger.warning("来源位置反查失败（不影响提取结果）: %s", e)
        return ""

# 提示词缓存预热等待（OpenBidKit 设为 5000ms）
PROMPT_CACHE_WARMUP_DELAY_MS = 5000


# ---------------------------------------------------------------------------
# ✅ 2026-09-30（第十一轮）：分段策略单一出口
# 均分模式（对齐 userTextSplitter.cjs）与滑动窗口模式的选择只在这一处判定，
# 避免两处各自判定「是否均分」的分叉（本仓 §4.3/§4.14 的同构教训）。
# 默认（bid_analysis_segment_even=False 或 context_length_limit<=0）走旧滑动
# 窗口，行为与引入前逐字一致。
# ---------------------------------------------------------------------------
def _split_tender_text(tender_text: str) -> list[str]:
    """按配置选择分段策略（唯一出口）。"""
    limit = _cfg_int("bid_analysis_segment_context_limit", 0)
    if _cfg_int("bid_analysis_segment_even", 0) and limit > 0:
        return split_for_analysis(tender_text, even=True,
                                  context_length_limit=limit)
    return split_for_analysis(tender_text)


def _cfg_int(name: str, default: int) -> int:
    """从 settings 读取整数配置。

    ⚠️ **不得写成** ``getattr(settings, name, default) or default`` ——
    那会把用户显式配置的 ``0`` 当成「未配置」而回落到 default
    （本轮 A/B 反向验证实测：配 0 并发被静默改成 2，与用户意图相反）。
    这里用显式 ``is None`` 判定，只有「属性不存在 / 值为 None」才回落默认。
    """
    value = getattr(settings, name, None)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _item_concurrency() -> int:
    """提取项级并发上限（默认 2，与既有硬编码一致；下限 1 —— 0 会死锁）。"""
    return max(1, _cfg_int("bid_analysis_item_concurrency", 2))


def _segment_concurrency() -> int:
    """单解析项内分段并发上限（默认 3，与既有硬编码一致；下限 1）。"""
    return max(1, _cfg_int("bid_analysis_segment_concurrency", 3))


def _item_retries() -> int:
    """单个解析项的 AI 重试次数（默认 2，与既有硬编码一致；下限 0）。"""
    return max(0, _cfg_int("bid_analysis_item_retries", 2))


# 分段结果合并 Prompt
# ✅ 2026-09-22 增强（对齐 OpenBidKit utils/segmentedAiResultMerger.cjs）：
#    合并调用必须同时携带【原始任务要求】（含 JSON 字段结构）。旧实现只给
#    「请输出形态一致」的口头约束、不给字段清单，实测 JSON 项（projectBasicInfo
#    有 37 个字段）在合并阶段会丢字段 —— 模型无从知道该保留哪些 key。
SEGMENT_MERGE_PROMPT = """以下是同一份项目资料不同分段对同一解析项的提取结果。每段结果只代表该片段内的信息，不代表整份文件的完整结论。

当前合并任务：__TASK_LABEL__

合并要求：
1. 如果某段写「没有提及」「原文未提及」「本段未提及」或整段只有「未提取到」，只表示该片段没有相关信息；如果其他片段提供了有效信息，应以有效信息为准。
2. 删除重复、空泛、冲突的片段性表述，保留更完整、更具体的信息。
3. 不要新增分段结果中没有的信息，不要自行编造。
4. 如果所有分段都没有有效信息，遵守原始任务中的整体无结果规则（Markdown 项输出「未提取到」，JSON 项各字段输出「没有提及」）。
5. 输出形态必须与原始任务要求完全一致：Markdown 项只输出整理后的 Markdown；JSON 项只输出一个 JSON 对象，**必须保留原始任务中列出的全部 key**，不得增删或改名。

原始任务要求：
__TASK_PROMPT__

分段解析结果：
__SEGMENTS__"""


# =========================================================================
# 1. 解析项定义（给前端展示用）
# =========================================================================
#: ⚠️ 2026-10-01 定位切换（招投标 → 专项施工方案）：招标响应域已**整域下线**。
#:
#: 它是 2026-09-30 对齐 OpenBidKit 易标时以「加法引入」的方式接入的，当时用
#: ``settings.bid_response_domain_enabled``（默认 False）做软开关。但软开关存在
#: 一个真实风险：一旦有人为了调试把它显式设为 True（或环境变量写错），招投标
#: 域就会**静默复活** —— 前端拿到 18 项招标响应清单、/start 真的能跑完评标
#: 方法/商务条款提取，而软件此时已明确定位为专项施工方案编写软件。
#: 硬门禁消除这条路径：无论配置怎么写，非 scheme 域一律 404。
#:
#: 注意区分两件事：
#:   - 招标响应域**代码**（BID_RESPONSE_ITEMS 等）保留 —— 删除它要同步清理
#:     主键双格式、下游跨域消费、73 个域测试，收益为零；
#:   - 招标响应域**入口**关闭 —— 这才是「去掉招投标功能」的实质。
_BID_RESPONSE_RETIRED_MSG = (
    "提取域 {domain} 已下线：本软件定位为「建筑工程专项施工方案编写软件」，"
    "招标响应分析（评标方法 / 商务条款 / 废标项等）不在服务范围内。"
    "请改用 domain=\"scheme\"（专项方案编制域）。"
)


def _assert_domain_available(domain: str) -> None:
    """校验提取域可用性（非 scheme 域一律 404）。

    所有暴露 ``domain`` 参数的端点必须调用本函数（唯一出口），避免门禁在
    某个入口漏设 —— 这是本仓反复出现的「同一判据多处各自实现」模式的反面教材。
    未知域**不在这里**处理：/items 对未知域返回空清单 + ``domain_unknown``
    （fail-closed 但非错误），由调用方自行判定。
    """
    d = domain if isinstance(domain, str) else ""
    d = d.strip()
    if not d or d == "scheme":
        return
    # 只在「该域确实注册过、但已随定位切换下线」时拦截。未知域**不在此处理**：
    # /items 对未知域返回空清单 + domain_unknown=True 是既有契约（fail-closed
    # 但不算错误），改它会让脚本调用方拿到 404 而非空清单。
    if d in EXTRACTION_DOMAINS:
        raise HTTPException(404, _BID_RESPONSE_RETIRED_MSG.format(domain=d))


@router.get("/items")
async def list_analysis_items(domain: str = "scheme"):
    """返回指定提取域的解析项定义（含必选标记、输出类型、分组）。

    同时返回 required_count / optional_count / markdown_count / json_count，
    供前端直接渲染「N 项（M 必选 + K 可选）」—— 此前前端硬编码「18 项」「17 必选」，
    增减解析项时文案失真（本轮已修正 api/index.ts 中残留的「20 项 / 14 项 / 15 分组」注释）。

    ✅ 2026-09-30（第十一轮）：新增 ``domain`` 参数（默认 "scheme"）。
      - ``domain="scheme"``：本软件 18 项，返回结构与旧版**逐字一致**；
      - ``domain="bid_response"``：⚠️ 2026-10-01 定位切换后已**硬门禁下线**
        （见 :func:`_assert_domain_available`）；
      - 未知 domain：返回空清单 + ``domain_unknown=true``（fail-closed，不静默回退）。
    """
    _assert_domain_available(domain)
    items = get_items_by_domain(domain)
    if not items:
        return {
            "items": [], "groups": [], "required_item_ids": [],
            "total": 0, "required_count": 0, "optional_count": 0,
            "markdown_count": 0, "json_count": 0, "group_count": 0,
            "domain": domain, "domain_unknown": True,
        }
    # ⚠️ scheme 域沿用 get_groups() / REQUIRED_ITEM_IDS / get_all_items()，
    #    保证既有调用点与测试看到的返回结构与旧版逐字一致。
    if domain == "scheme":
        items_list, groups_list, required_ids = get_all_items(), get_groups(), list(REQUIRED_ITEM_IDS)
    else:
        items_list = [dict(it) for it in items]
        groups_list = get_groups_by_domain(domain)
        required_ids = [it["item_id"] for it in items if it["required"]]
    return {
        "domain": domain,
        "items": items_list,
        "groups": groups_list,
        "required_item_ids": required_ids,
        "total": len(items_list),
        "required_count": len(required_ids),
        "optional_count": len(items_list) - len(required_ids),
        "markdown_count": sum(1 for it in items_list
                              if it.get("output_type") != "json"),
        "json_count": sum(1 for it in items_list
                          if it.get("output_type") == "json"),
        "group_count": len(groups_list),
        "domain_unknown": False,
    }


@router.get("/items/{item_id}")
async def get_analysis_item(item_id: str, domain: str = ""):
    """返回单个解析项的定义。

    ✅ 2026-09-30：返回值额外带 ``domain`` 与 ``fields`` 两个**加法式**键
    （前端可据此显示所属域与 JSON 字段清单；旧字段全部保留，不破坏既有断言）。
    显式传 ``domain`` 时校验归属，跨域访问返回 404。
    """
    item = get_item_def(item_id)
    if not item:
        raise HTTPException(404, f"解析项 {item_id} 不存在")
    item_domain = get_item_domain(item_id) or "scheme"
    # ⚠️ 2026-10-01 定位切换：属招标响应域的解析项（techRequirements /
    # businessScoring / 评标方法等）一律不可读 —— 即使调用方知道 item_id，
    # 也不应能从元数据端点把招标响应域「复活」。
    if item_domain != "scheme":
        raise HTTPException(404, _BID_RESPONSE_RETIRED_MSG.format(domain=item_domain))
    # 显式传 domain 时同样走统一门禁（与 /items 同口径，不各写一份判据）。
    _assert_domain_available(domain)
    if domain and domain != item_domain:
        raise HTTPException(404, "解析项 %s 属于 %s 域，不在 %s 域" % (
            item_id, item_domain, domain))
    result = dict(item)
    result["domain"] = item_domain
    result["fields"] = get_item_fields(item_id)
    return result


@router.get("/domains")
async def list_extraction_domains():
    """返回可用提取域清单及启用状态（前端据此决定是否展示域切换器）。

    ⚠️ 2026-10-01 定位切换：招标响应域恒 ``enabled=False`` 并附
    ``retired=True`` + ``retired_reason``，即使
    ``settings.bid_response_domain_enabled`` 被显式设为 True。前端据此可以
    把该域从切换器里隐藏，而不是渲染出一个点击后必然 404 的选项。
    """
    domains = []
    for name in EXTRACTION_DOMAINS:
        retired = (name != "scheme")
        enabled = (name == "scheme")
        items = get_items_by_domain(name)
        domains.append({
            "domain": name,
            "enabled": enabled,
            "retired": retired,
            "retired_reason": _BID_RESPONSE_RETIRED_MSG.format(domain=name)
            if retired else "",
            "total": len(items),
            "required_count": sum(1 for it in items if it["required"]),
            "label": "专项方案编制域" if name == "scheme" else "招标响应域（已下线）",
        })
    return {"domains": domains}


# =========================================================================
# 2. 多标段检测（对已解析的招标文件文本运行规则检测 + 可选 AI 识别）
# =========================================================================
@router.post("/check-sections")
async def check_bid_sections(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """检测该项目下的招标文件是否疑似多标段。

    规则检测（纯 Python，秒级），复用 bidSectionDetector.detect_bid_sections。
    若检测到多标段，返回检测结果 + 提示。
    """
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    # 读取该项目下所有已解析文档的合并文本（优先招标文件，其次合同/设计/地勘）
    docs = await _list_parsed_documents(db, real_pid)
    if not docs:
        return {"ok": True, "has_multiple": False, "source": "no_documents",
                **_combine_doc_texts_report([])[1]}

    # 把所有已解析文档拼接，同时产出「提取依据完整性」报告
    # （✅ 2026-09-26：报告透传给前端，让用户看到检测是否基于完整资料）
    combined_text, source_report = _combine_doc_texts_report(docs)
    if not combined_text.strip():
        return {"ok": True, "has_multiple": False, "source": "empty_text",
                **source_report}

    # 规则检测
    result = detect_bid_sections(combined_text)

    # 持久化检测结果（bid_sections 表）
    # ✅ BUG 修复：旧 SQL 三重不匹配 —— 列清单 7 个、VALUES 里 7 个 ? + datetime
    #    共 8 个值、参数只有 6 个。SQLite 直接抛「N values for M columns」，
    #    被 except 吞成 warning → 多标段检测结果从未成功落库，
    #    前端「检测历史」永远为空。现改为显式列出全部列，参数与占位符一一对应
    #    （12 列 = 11 个 ? + datetime，共 11 个参数）。
    # ✅ BUG 修复（2026-09-23，数据丢失）：上一次修复只写 7 列 —— SQLite 的
    #    REPLACE 语义是「先 DELETE 再 INSERT」，bid_sections 的
    #    selected_section_id / selected_section_title / selected_section_json /
    #    status / error 全部回落 DEFAULT。用户已选定的投标范围会被下一次点击
    #    「多标段检测」按钮**静默清零**，随后 resolve_section_hint 返回空 hint，
    #    AI 提取又把其它标段的参数混着抽进本次方案，且无任何告警。
    #    现与 POST /extract-sections 同口径：先读出现有行，把「选择」类字段原样
    #    回写，只更新「检测」类字段（detected_sections / is_multi / total_declared）。
    try:
        row = await load_bid_section_row(db, real_pid, scheme_id)
        sec_id = row.get("id") or f"{real_pid}_default"
        await db.execute(
            "INSERT OR REPLACE INTO bid_sections "
            "(id, project_id, scheme_id, is_multi, total_declared, detected_sections, "
            " selected_section_id, selected_section_title, selected_section_json, "
            " status, error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (sec_id, real_pid, scheme_id or row.get("scheme_id") or "",
             1 if result.get("has_multiple") else 0,
             result.get("total_declared") or 0,
             json.dumps(result.get("sections") or [], ensure_ascii=False),
             row.get("selected_section_id") or "",
             row.get("selected_section_title") or "",
             row.get("selected_section_json") or "",
             row.get("status") or "idle", row.get("error") or ""))
        await db.commit()
    except Exception as e:
        logger.warning("多标段检测结果持久化失败: %s", e)

    # ✅ 选择状态回传（2026-09-23）：检测会重写 bid_sections 行，若不清空选择则必须
    #    如实告知前端「当前投标范围仍为 X」，否则用户以为重检后需要重选。
    selected = selected_section_from_row(row)
    return {
        "ok": True,
        "has_multiple": result.get("has_multiple", False),
        "total_declared": result.get("total_declared"),
        "detected_count": result.get("detected_count", 0),
        "sections": result.get("sections", []),
        "by_total": result.get("by_total", False),
        "doc_sources": [d.get("file_name", "") for d in docs],
        "selected_section_id": selected.get("id", ""),
        "selected_section_title": selected.get("title", ""),
        "needs_selection": bool(result.get("has_multiple")) and not bool(selected),
        # ✅ 2026-09-26：检测依据的完整性（被截断的文档清单 / 预算耗尽被整份
        #    跳过的文档数）。多标段检测建立在合并文本之上，资料不完整时
        #    「疑似多标段」的结论可能失真，前端据此提示用户回解析提取模块补资料。
        **source_report,
    }


# =========================================================================
# 2.5 投标范围（标段）选择 —— 对齐 OpenBidKit bidSectionDetector/Context
# =========================================================================
@router.get("/bid-sections")
async def get_bid_sections(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """查询多标段检测结果与当前选中的投标范围。

    ✅ 新增（2026-09-22）：前端「提取项目」需要知道①是否多标段、
    ②当前选中的是哪个标段、③该选择会被如何注入 AI（提示词预览），
    才能让用户在多标段场景下明确指定投标范围。
    """
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    row = await load_bid_section_row(db, real_pid, scheme_id)
    section = selected_section_from_row(row)
    hint = build_bid_section_context_hint(section, has_selected_section=bool(section))
    try:
        detected = json.loads(row.get("detected_sections") or "[]")
    except (json.JSONDecodeError, TypeError):
        detected = []
    return {
        "ok": True,
        "is_multi": bool(row.get("is_multi")),
        "total_declared": int(row.get("total_declared") or 0),
        "detected_sections": detected if isinstance(detected, list) else [],
        "selected_section_id": row.get("selected_section_id") or "",
        "selected_section_title": row.get("selected_section_title") or "",
        "selected_section": section,
        "context_hint": hint,
        # 多标段但未选择 → 前端应提示用户「请选择本次投标范围」，否则 AI 会
        # 把各标段参数混在一起（本软件默认不注入提示以保持旧行为）。
        "needs_selection": bool(row.get("is_multi")) and not bool(section),
    }


@router.post("/select-section")
async def select_bid_section(body: dict,
                             scheme_id: str = Query(""),
                             project_id: str = Query(""),
                             db=Depends(get_db)):
    """选择 / 清除本次投标范围（多标段项目的必需前置步骤）。

    ✅ 新增（2026-09-22，对齐 OpenBidKit `technicalPlanStore.tenderFile.selectedSectionId`）：
    选定后，后续「提取项目」的每一次 AI 调用都会带上「当前只处理 X 标段」的
    system 提示（见 `services/bid_section_context.build_bid_section_context_hint`），
    避免把其它标段的参数抽进本次方案。

    请求体（section_id 为空 → 清除选择，恢复旧行为）：
      {
        "scheme_id": "...", "project_id": "...",
        "section_id": "section-2",            # 空串 = 清除
        "section_title": "二标段",             # 可选
        "head_line": "...", "description": "...", "evidence": ["..."]
      }

    契约：
      - 未做过多标段检测（无 bid_sections 行）时也允许写入（前端可直接选）；
      - 选择变更只影响**后续** AI 调用，不自动重跑已完成的解析项
        （与 OpenBidKit 一致：避免用户在不知情的情况下被批量重解析烧额度）。

    ✅ BUG 修复（2026-09-23，前后端契约断裂）：前端 `bidAnalysisApi.selectSection`
       把 scheme_id / project_id 放在 **URL query**（与本模块 check-sections /
       results / updateResult 等端点完全同口径），而旧实现只从 JSON body 读 ——
       真实前端调用 100% 命中「需要 scheme_id 或 project_id」的 400，该能力实际
       从未可用（好在当时也没有 UI 调用点，属潜伏雷）。现改为 query 优先、
       body 兜底：既有脚本/单测把 scheme_id 放进 body 的写法继续可用，
       前端 query 写法也开始生效。
    """
    body_scheme = str((body or {}).get("scheme_id") or "").strip()
    body_project = str((body or {}).get("project_id") or "").strip()
    # ✅ 与 _resolve_pid 同口径：直接以函数方式调用本端点（单测/脚本）时，
    #    未传参的 Query("") 会保留 Query 对象作为默认值，直接 .strip() 会
    #    AttributeError。这里先做 isinstance 收窄，再走 body 兜底。
    scheme_id = scheme_id.strip() if isinstance(scheme_id, str) else ""
    project_id = project_id.strip() if isinstance(project_id, str) else ""
    scheme_id = scheme_id or body_scheme
    real_pid = await _resolve_pid(db, scheme_id, project_id or body_project)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    section_id = str(body.get("section_id") or "").strip()
    section: dict = {}
    if section_id:
        section = {
            "id": section_id,
            "title": str(body.get("section_title") or body.get("title") or "").strip(),
            "headLine": str(body.get("head_line") or body.get("headLine") or "").strip(),
            "description": str(body.get("description") or "").strip(),
            "evidence": [str(e).strip() for e in (body.get("evidence") or [])
                         if str(e).strip()][:6] if isinstance(body.get("evidence"), list)
                        else [],
        }

    row = await load_bid_section_row(db, real_pid, scheme_id)
    sec_pk = row.get("id") or f"{real_pid}_default"
    try:
        await db.execute(
            "INSERT OR REPLACE INTO bid_sections "
            "(id, project_id, scheme_id, is_multi, total_declared, detected_sections, "
            " selected_section_id, selected_section_title, selected_section_json, "
            " status, error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (sec_pk, real_pid, scheme_id or row.get("scheme_id") or "",
             int(row.get("is_multi") or 0), int(row.get("total_declared") or 0),
             row.get("detected_sections") or "[]",
             section.get("id", ""), section.get("title", ""),
             json.dumps(section, ensure_ascii=False) if section else "",
             row.get("status") or "idle", row.get("error") or ""))
        await db.commit()
    except Exception as e:
        logger.exception("保存施工范围选择失败")
        raise HTTPException(500, f"保存施工范围失败：{e}")

    hint = build_bid_section_context_hint(section, has_selected_section=bool(section))
    logger.info("项目 %s 施工范围更新为：%s（提示注入：%s）",
                real_pid, section.get("title") or "<未选择>",
                "是" if hint else "否")
    return {
        "ok": True,
        "selected_section_id": section.get("id", ""),
        "selected_section_title": section.get("title", ""),
        "selected_section": section,
        "context_hint": hint,
        "cleared": not bool(section),
    }


@router.post("/extract-sections")
async def extract_bid_sections_api(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """AI 识别招标文件标段清单（供「选择投标范围」使用）。

    ✅ 新增（2026-09-22，对齐 OpenBidKit `bidSectionExtractionTask.cjs`）：

    与 `POST /check-sections` 的分工：
      - check-sections   ：纯规则、零成本，只回答「是否**疑似**多标段」（秒级）；
      - extract-sections ：AI 结构化识别，产出可选择的标段清单（标题/描述/
        依据/**原文行号区间**），用户据此在 `POST /select-section` 精确指定
        本次投标范围。

    调用成本：分段数 + 1 次 AI 调用（超长招标文件请先确认确实需要）。
    识别结果落 `bid_sections.detected_sections`，不自动改变已有选择。
    """
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    docs = await _list_parsed_documents(db, real_pid)
    if not docs:
        raise HTTPException(400, "请先在「上传解析」上传文件并完成解析后再执行标段识别")
    text, source_report = _combine_doc_texts_report(docs)
    if not text.strip():
        raise HTTPException(400, "已解析文档未包含有效文本内容")

    from app.services.bid_section_extraction import extract_bid_sections
    try:
        result = await extract_bid_sections(markdown=text)
    except ValueError as e:
        # 未识别到 ≥2 个有效标段（参考实现同口径）：属输入/内容问题而非服务故障
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.exception("AI 标段识别失败")
        raise HTTPException(500, f"标段识别失败：{e}")

    sections = result["sections"]
    # 落库：保持既有选择（selected_* 不动），只更新检测结果
    row = await load_bid_section_row(db, real_pid, scheme_id)
    sec_pk = row.get("id") or f"{real_pid}_default"
    try:
        await db.execute(
            "INSERT OR REPLACE INTO bid_sections "
            "(id, project_id, scheme_id, is_multi, total_declared, detected_sections, "
            " selected_section_id, selected_section_title, selected_section_json, "
            " status, error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (sec_pk, real_pid, scheme_id or row.get("scheme_id") or "", 1,
             len(sections), json.dumps(sections, ensure_ascii=False),
             row.get("selected_section_id") or "",
             row.get("selected_section_title") or "",
             row.get("selected_section_json") or "",
             "success", ""))
        await db.commit()
    except Exception as e:
        # 落库失败不影响本次识别结果的使用（前端仍可展示并选择）
        logger.warning("标段识别结果落库失败: %s", e)

    selected = selected_section_from_row(row)
    msg = (f"已识别 {len(sections)} 个标段（{result['segment_count']} 段，"
           f"约 {result['estimated_calls']} 次模型调用），请确认本次施工范围")
    logger.info("项目 %s AI 标段识别完成：%d 个标段", real_pid, len(sections))
    return {
        "ok": True,
        "sections": sections,
        "count": len(sections),
        "segment_count": result["segment_count"],
        "estimated_calls": result["estimated_calls"],
        "selected_section_id": selected.get("id", ""),
        "selected_section_title": selected.get("title", ""),
        "context_hint": build_bid_section_context_hint(
            selected, has_selected_section=bool(selected)),
        "message": msg,
        # ✅ 2026-09-26：AI 标段识别同样建立在合并文本之上，透传依据完整性
        **source_report,
    }


# =========================================================================
# 3. 已存储的解析结果（CRUD）
# =========================================================================
@router.get("/results")
async def list_analysis_results(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """查询该项目/方案下已存储的 18 项提取结果。

    过滤掉历史版本遗留的 item_id（如旧版 18 项招标项），避免汇总计数虚高。
    """
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    # ✅ 用 SELECT * 而非显式列清单：bid_analysis_items 的 source 列由
    #    db._migrate 增量补列，若某环境迁移失败（权限/只读库），显式列会直接
    #    抛 "no such column: source" 让整个结果查询 500；SELECT * 对缺列免疫。
    cur = await db.execute(
        "SELECT * FROM bid_analysis_items WHERE project_id=? ORDER BY sort_order",
        (real_pid,))
    known_ids = {it["item_id"] for it in ANALYSIS_ITEMS}
    rows = [dict(r) for r in await cur.fetchall() if r["item_id"] in known_ids]
    # ✅ 关联项目展示：逐行附来源方案名（发起提取的方案；结果本体项目级共享）
    await _attach_scheme_names(db, rows)

    # ✅ 技术债清理（2026-09-24）：旧实现在此手写了一份汇总（total/success/
    #    errors/running/manual/missing_required/success_valid），但返回值从未使用
    #    这些局部变量——汇总唯一口径是 _compute_results_summary（必选项空内容计
    #    缺失的 is_missing_result 口径在其中统一维护），手写副本只会误导维护者。
    return {
        "items": rows,
        "summary": _compute_results_summary(rows),
    }


@router.get("/results/{item_id}")
async def get_single_result(
    item_id: str,
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """查询单个解析项的结果。"""
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    cur = await db.execute(
        "SELECT * FROM bid_analysis_items WHERE project_id=? AND item_id=?",
        (real_pid, item_id))
    row = await cur.fetchone()
    if not row:
        # 未生成过：返回 item_id 对应的定义 + idle 状态
        item = get_item_def(item_id)
        if not item:
            raise HTTPException(404, f"解析项 {item_id} 不存在")
        return {**item, "status": "idle", "content": "", "error": ""}
    result = dict(row)
    await _attach_scheme_names(db, [result])
    return result


def _compute_results_summary(rows: list[dict]) -> dict:
    """计算 /results 的 summary 块（list_analysis_results 与人工校正端点共用）。

    为什么抽成独立函数：人工校正（PUT /results/{item_id}）写库后必须立刻回一份
    与列表口径完全一致的 summary。若复制一份汇总逻辑，两边「必选项是否缺失」的
    判定会分叉 —— 这正是本模块历史上反复出现的前后端/双入口分叉问题。
    """
    total = len(ANALYSIS_ITEMS)
    success = sum(1 for r in rows if r["status"] == "success")
    errors = sum(1 for r in rows if r["status"] == "error")
    running = sum(1 for r in rows if r["status"] == "running")
    # 人工校正计数：source='manual' 的行数（老库无该列时为 0）
    manual = sum(1 for r in rows if r.get("source") == "manual")

    # 检查必选项是否全部成功，且**内容有效**
    missing_required = []
    for rid in REQUIRED_ITEM_IDS:
        def_ = get_item_def(rid) or {}
        matched = next((r for r in rows if r["item_id"] == rid), None)
        if (not matched or matched["status"] != "success"
                or not matched.get("content")
                or is_missing_result(matched.get("content", ""),
                                     matched.get("output_type",
                                                  def_.get("output_type", "markdown")))):
            missing_required.append(def_.get("label", rid))

    # 「有有效内容的完成项」数：status=success 且 is_missing_result 为假。
    # 与 missing_required 同口径（is_missing_result），供前端放行下游步骤。
    success_valid = 0
    for r in rows:
        if r["status"] != "success" or not r.get("content"):
            continue
        def_ = get_item_def(r["item_id"]) or {}
        if is_missing_result(r["content"],
                             r.get("output_type") or def_.get("output_type", "markdown")):
            continue
        success_valid += 1

    return {
        "total": total,
        "success": success,
        "errors": errors,
        "running": running,
        "pending": total - success - errors - running,
        "progress": round((success + errors) / total, 4) if total else 0,
        "all_required_done": len(missing_required) == 0,
        "missing_required": missing_required,
        # ✅ 新增：真正「有有效内容」的完成数（排除 content='未提取到' / json 全空）。
        #    前端「下一步：目录生成」此前用 success>0 放行，会把一个只有空标记的
        #    完成项当成可用成果，直接进入目录生成。
        "success_valid": success_valid,
        # ✅ 新增：人工校正计数（source='manual'），供前端显示校正徽标
        "manual_count": manual,
    }


async def _load_results_rows(db, project_id: str) -> list[dict]:
    """按 project_id 读取 bid_analysis_items（仅保留已知解析项），供查询/校验端点复用。"""
    cur = await db.execute(
        "SELECT * FROM bid_analysis_items WHERE project_id=? ORDER BY sort_order",
        (project_id,))
    known_ids = {it["item_id"] for it in ANALYSIS_ITEMS}
    return [dict(r) for r in await cur.fetchall() if r["item_id"] in known_ids]


async def _attach_scheme_names(db, rows: list[dict]) -> None:
    """给结果行附加来源方案名（scheme_id → schemes.name）。

    ✅ 2026-09-23「关联项目」展示口径：提取结果以 project_id 为准全项目共享，
    逐行能展示的关联关系只有「是哪个方案发起的这次提取」。查不到/缺列时静默
    跳过（scheme_name 缺席不阻断结果查询）。
    """
    sids = sorted({str(r.get("scheme_id") or "") for r in rows if r.get("scheme_id")})
    if not sids:
        return
    try:
        placeholders = ",".join("?" for _ in sids)
        cur = await db.execute(
            f"SELECT id, name FROM schemes WHERE id IN ({placeholders})",
            tuple(sids))
        names = {str(r["id"]): (r["name"] or "") for r in await cur.fetchall()}
        for r in rows:
            r["scheme_name"] = names.get(str(r.get("scheme_id") or ""), "")
    except Exception as e:
        logger.warning("附加来源方案名失败（不影响结果查询）: %s", e)


# =========================================================================
# 3.5 人工校正 / 清空单个解析项
# =========================================================================
@router.put("/results/{item_id}")
async def update_single_result(
    item_id: str,
    body: dict,
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """人工校正单个解析项的提取结果（覆盖 AI 输出，标记 source='manual'）。

    为什么需要：AI 抽错关键参数（如基坑深度、支护形式）时，旧实现唯一的出路是
    整项重跑（贵且结果不稳定），而抽错的值会继续被下游「目录生成 / 正文生成」
    消费（sse_handlers 走 format_downstream_context）。人工校正是唯一可靠出口，
    与 global-facts 的 /resolve 裁决对齐。

    契约：
      - body.content 必填（字符串）；json 项必须是可解析的 JSON 对象/数组；
      - 空字符串 / 纯空白 → 422（想清空请用 DELETE）；
      - content 恰为「未提取到」时按「无有效内容」处理，仍写入但 summary 会判缺失；
      - 写入后 status='success'、error 清空、source='manual'。
    """
    if item_id not in {it["item_id"] for it in ANALYSIS_ITEMS}:
        raise HTTPException(404, f"解析项 {item_id} 不存在")
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(422, "content 不能为空（需要清空请改用 DELETE）")

    def_ = get_item_def(item_id) or {}
    output_type = def_.get("output_type", "markdown")
    if output_type == "json":
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise HTTPException(422, f"JSON 解析项必须提交合法 JSON：{e}")
        if isinstance(parsed, str):
            raise HTTPException(422, "JSON 解析项必须是对象或数组，不能是裸字符串")

    # ✅ 主键经 build_item_pk 唯一出口构造（scheme 域返回历史的
    #    {project_id}_{item_id} 格式，字节不变）——旧实现在此手写 f-string
    #    副本，与写入侧其他 6 处调用点口径并存，属本仓反复出现的判据分叉。
    resolved_domain = get_item_domain(item_id) or "scheme"
    pk = build_item_pk(real_pid, item_id, resolved_domain)
    # ⚠️ G3 根因加固（R45）：schemes.project_id 的唯一写入守卫。在 try **之外**
    # 计算 —— 本函数的 except 会把 HTTPException 也吞成 500（HTTPException 是
    # Exception 子类），必须避免 422 被降级。
    # _resolve_pid 已 strip 且防空（空则 404），此处显式再过一遍，让「写
    # schemes.project_id 必须过 require_project_id」的不变量在每个写点可见 ——
    # 护栏 test_schemes_project_id_guard_20261006.py 按此扫描全仓写路径。
    real_pid = require_project_id(real_pid)
    try:
        if scheme_id:
            await db.execute(
                "UPDATE schemes SET project_id=? WHERE id=?", (real_pid, scheme_id))
        cur = await db.execute(
            "UPDATE bid_analysis_items SET content=?, status='success', error='', "
            "source='manual', evidence='', "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (content, pk))
        changed = safe_rowcount(cur, what="人工校正 UPDATE bid_analysis_items")
        if not changed:
            # ✅ BUG 修复（静默丢失）：从未提取过的项在库中没有行（行由首次
            #    提取/force_rerun 时才补齐），UPDATE 命中 0 行后接口仍返回
            #    {"item": null, "summary": ...} 且无任何错误 —— 用户提交的校正
            #    内容静默丢弃，刷新后全部丢失。现补 INSERT（与
            #    _update_item_status 的兼容旧库缺 domain 列同口径降级）。
            cur_cols = await _bid_analysis_item_cols(db)
            if "domain" in cur_cols:
                await db.execute(
                    "INSERT INTO bid_analysis_items "
                    "(id, project_id, scheme_id, item_id, label, output_type, required, "
                    "status, content, error, sort_order, source, evidence, domain) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pk, real_pid, scheme_id or "", item_id, def_.get("label", item_id),
                     def_.get("output_type", "markdown"), def_.get("required", 0),
                     "success", content, "", def_.get("sort_order", 0),
                     "manual", "", resolved_domain))
            else:
                await db.execute(
                    "INSERT INTO bid_analysis_items "
                    "(id, project_id, scheme_id, item_id, label, output_type, required, "
                    "status, content, error, sort_order, source, evidence) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pk, real_pid, scheme_id or "", item_id, def_.get("label", item_id),
                     def_.get("output_type", "markdown"), def_.get("required", 0),
                     "success", content, "", def_.get("sort_order", 0),
                     "manual", ""))
        await db.commit()
        logger.info("人工校正解析项 %s.%s（%d 字符，%s）",
                    real_pid, item_id, len(content),
                    "更新既有行" if changed else "补齐缺失行")
    except Exception as e:
        logger.error("人工校正解析项失败: %s", e)
        raise HTTPException(500, f"更新失败: {e}")

    rows = await _load_results_rows(db, real_pid)
    updated = next((r for r in rows if r["item_id"] == item_id), None)
    # ✅ 人工校正同样属于「提取结果变化」→ 下游导出缓存必须失效
    await _invalidate_downstream_cache(db, real_pid, scheme_id)
    return {"item": updated, "summary": _compute_results_summary(rows)}


@router.delete("/results/{item_id}")
async def clear_single_result(
    item_id: str,
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """清空单个解析项的结果（回到 idle，等待重新提取）。

    人工校正的撤销路径：content 清空、status 回 idle、source 回 'ai'。
    不删除行本身 —— 行是「解析项槽位」，删掉会破坏 sort_order 展示顺序与
    force_rerun 的 upsert 语义（_reset_items_for_rerun 会重新 INSERT）。
    """
    if item_id not in {it["item_id"] for it in ANALYSIS_ITEMS}:
        raise HTTPException(404, f"解析项 {item_id} 不存在")
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    # ✅ 主键唯一出口（与 update_single_result 同口径，scheme 域字节不变）
    pk = build_item_pk(real_pid, item_id, get_item_domain(item_id) or "scheme")
    await db.execute(
        "UPDATE bid_analysis_items SET content='', error='', status='idle', "
        "source='ai', evidence='', "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (pk,))
    await db.commit()
    rows = await _load_results_rows(db, real_pid)
    updated = next((r for r in rows if r["item_id"] == item_id), None)
    # ✅ 清空单项 → 下游输入变化 → 导出缓存失效（与人工校正同口径）
    await _invalidate_downstream_cache(db, real_pid, scheme_id)
    return {"item": updated, "summary": _compute_results_summary(rows)}


# =========================================================================
# 4. 启动解析（SSE 长任务）
# =========================================================================
@router.post("/start")
async def start_bid_analysis(
    body: dict,
    db=Depends(get_db),
):
    """启动 18 项结构化提取（非 SSE 同步入口，用于简单测试）。

    正式使用 /start-sse 获取实时进度推送。
    """
    scheme_id = body.get("scheme_id", "")
    project_id = body.get("project_id", "")
    mode = body.get("mode", "key")
    selected_item_ids = body.get("selected_item_ids") or []
    force_rerun = body.get("force_rerun", False)
    # ✅ 2026-09-30（第十一轮）：提取域 + 断点续跑（默认值与旧版行为逐字一致）
    domain = body.get("domain", "scheme")
    # ⚠️ 2026-10-01：招标响应域已下线，硬门禁拒绝（与 /items 同口径）
    _assert_domain_available(domain)
    skip_done = bool(body.get("skip_done") or settings.bid_analysis_skip_done_when_rerun)

    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    config = AnalysisConfig(mode=mode, selected_item_ids=selected_item_ids,
                            force_rerun=force_rerun, domain=domain,
                            skip_done=skip_done).normalize()
    if not await config.get_task_items_async(db, real_pid):
        raise HTTPException(400, "未选择任何解析项")

    # 校验输入
    docs = await _list_parsed_documents(db, real_pid)
    if not docs:
        raise HTTPException(400, "请先在「上传解析」上传文件并完成解析后再执行结构化提取")

    # 读取招标文件文本（同时产出提取依据完整性报告）
    tender_text, source_report = _combine_doc_texts_report(docs)
    if not tender_text.strip():
        raise HTTPException(400, "已解析文档未包含有效文本内容")

    # ✅ 启动前清理中断残留（同 SSE 入口）：避免旧 running 项永久卡住
    await clear_interrupted_items(db, real_pid)

    # ✅ 多标段：读取当前投标范围并生成上下文提示（未选择 → 空串，行为不变）
    section_hint, _section = await resolve_section_hint(db, real_pid, scheme_id)

    # 直接执行（同步等待）
    try:
        result = await _run_bid_analysis_sync(
            db, real_pid, scheme_id or real_pid, config, tender_text,
            section_hint=section_hint)
        # ✅ 2026-09-26：非 SSE 入口同样回传提取依据完整性（截断文档清单等）
        result.update(source_report)
        return result
    except HTTPException:
        # ✅ BUG 修复：内层抛出的 400（缺参/无文档）被宽异常捕获后伪装成
        #    500，前端把「参数不对」当服务端崩溃重试/报警。语义错误原样上抛。
        raise
    except Exception as e:
        logger.exception("结构化解析 /start 崩溃")
        raise HTTPException(500, detail=str(e))


@router.get("/start-sse")
async def start_bid_analysis_sse(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    mode: str = Query("key"),
    selected_item_ids: str = Query(""),  # JSON 数组字符串
    force_rerun: bool = Query(False),
    domain: str = Query("scheme"),
    skip_done: bool = Query(False),
    db=Depends(get_db),
):
    """启动 18 项结构化提取（SSE 长任务，实时推送进度）。

    ✅ 2026-09-30（第十一轮）：新增 ``domain``（默认 scheme，与旧版一致）与
    ``skip_done``（默认 False，与旧版一致）。
    """
    try:
        return await _start_sse_inner(db, scheme_id, project_id, mode,
                                       selected_item_ids, force_rerun,
                                       domain=domain, skip_done=skip_done)
    except HTTPException:
        # ✅ BUG 修复：同上——_start_sse_inner 的 400 参数校验错误不得降级成 500
        raise
    except Exception as e:
        logger.exception("结构化解析 SSE 入口崩溃")
        raise HTTPException(500, detail=str(e))


async def _start_sse_inner(db, scheme_id, project_id, mode,
                           selected_item_ids, force_rerun,
                           domain: str = "scheme", skip_done: bool = False):
    # ⚠️ 2026-10-01：招标响应域已下线，硬门禁拒绝（与 /items、/start 同口径）。
    # 放在最前面：门禁用例不需要真的传文档就能验证，也避免门禁之后才做校验。
    _assert_domain_available(domain)
    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    # 校验输入
    docs = await _list_parsed_documents(db, real_pid)
    if not docs:
        raise HTTPException(400, "请先在「上传解析」上传文件并完成解析后再执行结构化提取")

    # ✅ 2026-09-26（调用链路补齐）：启用 report_truncation 分支 —— 该分支此前
    #    自加入起从未被调用，截断信号停在后端、前端"项目提取"子页无感知。
    tender_text, source_report = _combine_doc_texts_report(docs)
    if not tender_text.strip():
        raise HTTPException(400, "已解析文档未包含有效文本内容")

    # 解析 selected_item_ids
    sel_ids = []
    if selected_item_ids:
        try:
            sel_ids = json.loads(selected_item_ids)
        except (json.JSONDecodeError, TypeError):
            sel_ids = []

    config = AnalysisConfig(mode=mode, selected_item_ids=sel_ids,
                            force_rerun=force_rerun, domain=domain,
                            skip_done=skip_done).normalize()

    # ✅ 防呆：归一化后没有任何可执行项（如 mode=item 但未选/选了非法 id）时，
    #    直接 400。否则会跑出「0/0 完成」的假成功，前端任务栏与必选校验口径全乱。
    #    ⚠️ 此处显式 skip_done=False 做「选择集合」校验，与运行时是否跳过已完成
    #    项解耦 —— 否则「全部已完成 + 开启断点续跑」会被误报成「未选择任何解析项」。
    if not config.get_task_items():
        raise HTTPException(400, "未选择任何解析项")

    # ✅ 顺序（2026-09-20 BUG 修复）：先 register_task → 再 clear_interrupted_items
    #    → 最后 _reset_items_for_rerun。
    #    旧顺序是「先清空已提取结果，再注册任务」：register_task 一旦抛异常
    #    （DB 只读 / 迁移失败 / 内存态异常），已清空的解析项就永久丢失，且本轮
    #    不会重跑 —— 用户一次点击「开始提取」就丢掉全部 18 项既有成果。
    #    新顺序下任务先占位，清理与重置都发生在任务生命周期之内。
    task_id = await register_task(
        "bid_analysis",
        project_id=real_pid,
        scheme_id=scheme_id,
    )

    # ✅ 注册之后再清理「中断残留」：register_task 已把同 scheme 的旧 running
    #    任务判为 failed 并触发 stop，此刻仍为 running 的解析项都是被中断的
    #    孤儿（进程重启 / 客户端断开 / 用户停止）。不清理则 UI 永久显示
    #    「运行中」、summary.running>0、必选项永远判缺失 → 「18 项未完成」。
    await clear_interrupted_items(db, real_pid)

    # 最后清空**本次要跑的项**（不牵连用户未选中的项）
    if force_rerun:
        await _reset_items_for_rerun(
            db, real_pid, [it["item_id"] for it in config.get_task_items()])

    # ✅ 多标段：读取当前投标范围并生成上下文提示（未选择 → 空串，行为不变）
    section_hint, selected_section = await resolve_section_hint(db, real_pid, scheme_id)
    _sec_row = await load_bid_section_row(db, real_pid, scheme_id)
    if _sec_row.get("is_multi") and not selected_section:
        # 多标段但未选择：不注入提示（保持旧行为），但必须让用户知道
        logger.warning("项目 %s 为多标段但尚未选择投标范围，本次提取未注入标段上下文",
                       real_pid)

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()

        async def progress_callback(item_id: str, status: str, content: str = "",
                                    error: str = "", progress: float = 0):
            evt = {
                "type": "item_update",
                "item_id": item_id,
                "status": status,
                "progress": progress,
            }
            if content:
                # ✅ 发送完整 content（非仅 200 字预览）：前端列表项点击即可实时查看
                #    刚解析完成的完整内容；单项内容通常 < 10KB，SSE 完全可承载。
                evt["content"] = content
                evt["content_preview"] = content[:200]
            if error:
                evt["error"] = error
            await queue.put(evt)

        async def stats_callback(stats: dict):
            """推送「提取规模」：解析项数 × 切段数 ≈ 模型调用次数。

            ✅ 2026-09-20：超长文档（50 万字 = 32 段 × 18 项 = 576 次调用）
            此前对用户完全不可见，只能等十几分钟后从账单里发现额度被烧光。

            ✅ 2026-09-22：附带投标范围状态（是否已注入标段上下文 / 是否待选择），
            让前端能在同一条事件里提示「本次提取只针对 X 标段」。

            ✅ 2026-09-26：附带提取依据完整性（source_truncated / truncated_docs /
            dropped_doc_count），让前端在提取开始时就提示「资料不完整」。
            """
            await queue.put({
                "type": "text_stats", **stats, **source_report,
                "section_title": selected_section.get("title", ""),
                "section_hint_applied": bool(section_hint),
                "multi_section_unselected": bool(_sec_row.get("is_multi")) and not selected_section,
            })

        async def run_inner():
            try:
                result = await _run_bid_analysis_with_progress(
                    db, real_pid, scheme_id or real_pid, config, tender_text,
                    progress_callback, task_id, stats_callback=stats_callback,
                    section_hint=section_hint)
                await queue.put({"type": "completed", "result": result,
                                 **source_report})
            except asyncio.CancelledError:
                # ✅ 取消（客户端断开 / event_generator 的 finally 会 task.cancel()）：
                #    CancelledError 属 BaseException，**不会**走下面的 except Exception，
                #    本轮在途项会永久停在 running —— 这里显式收口。
                with contextlib.suppress(Exception):
                    await clear_interrupted_items(db, real_pid)
                await queue.put({"type": "cancelled", "message": "任务已取消"})
            except Exception as e:
                logger.exception("结构化解析异常")
                # ✅ 异常收尾同样收口：不留 running 孤儿（历史上 finish_task 的
                #    TypeError 正是在此被捕获，然后留下一批「运行中」的解析项）
                with contextlib.suppress(Exception):
                    await clear_interrupted_items(db, real_pid)
                await queue.put({"type": "error", "error": str(e)})
            finally:
                await queue.put({"type": "__done__"})

        task = asyncio.create_task(run_inner())

        try:
            async def _pump():
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=60)
                    except asyncio.TimeoutError:
                        # 60 秒无新事件，发送心跳式的任务状态
                        # ✅ BUG 修复：task_registry **没有 get_task()**（只有
                        #    get_task_stats/get_task_started_at/get_task_elapsed_ms），
                        #    旧实现 import 恒抛 ImportError 且被 except 吞掉 →
                        #    「60s 任务状态心跳」从未发出，前端 type==='heartbeat' 为死分支。
                        #    现直接读内存任务态。
                        try:
                            from app.services.ai import task_registry as _tr
                            t = _tr._tasks.get(task_id)
                            if t:
                                yield f"data: {json.dumps({'type': 'heartbeat', 'progress': t.get('progress', 0), 'message': t.get('message', '')}, ensure_ascii=False)}\n\n"
                        except Exception:
                            pass
                        continue
                    if evt.get("type") == "__done__":
                        break
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

            # 心跳包装：with_heartbeat 会自动每 10s 发心跳注释防连接超时
            # ✅ P0 断连修复（2026-09-23）：必须显式持有并在 finally 关闭 ——
            #    直接 `async for ... in with_heartbeat(...)` 遇 GeneratorExit
            #    离开循环时不会关闭内层流（async for 不负责关迭代器），内层
            #    两 Task 与 _pump 要等 GC 才收尾，断开期收尾时序不可控。
            _hb_stream = with_heartbeat(_pump())
            try:
                async for chunk in _hb_stream:
                    yield chunk
            finally:
                try:
                    await _hb_stream.aclose()
                except Exception:
                    logger.warning("bid_analysis: 关闭心跳流失败（已忽略）", exc_info=True)
        finally:
            if not task.done():
                task.cancel()
                # ✅ P0 断连修复（2026-09-23）：等 run_inner 真正退出再收敛事务，
                #    避免其被打断的写（execute 后、commit 前）晚于 settle 落下。
                try:
                    await asyncio.wait({task}, timeout=5)
                except asyncio.CancelledError:
                    logger.info("bid_analysis 收尾等待 run_inner 取消时被取消（task=%s）",
                                task_id)
                except Exception:
                    logger.warning("bid_analysis 等待 run_inner 取消失败（task=%s）", task_id,
                                   exc_info=True)
            # ✅ P0 断连修复（2026-09-23）：收敛全局共享连接上的悬挂写事务，
            #    否则全站写 500 database is locked。shield 保证回滚后台完成。
            try:
                await asyncio.shield(settle_global_conn("bid_finally"))
            except asyncio.CancelledError:
                logger.info("bid_analysis 收尾事务收敛已 shield（task=%s）", task_id)
            except Exception:
                logger.warning("bid_analysis 收尾事务收敛失败（task=%s）", task_id,
                               exc_info=True)
            # ✅ 断开兜底（2026-09-17 新增）：客户端断开时生成器被 aclose，
            #    GeneratorExit 直接落在 yield 处（不经过任何 except），
            #    run_inner 可能还没来得及调 finish_task —— 任务永远停在 running。
            #    sse_handlers 的三个生成器（outline/content/retry）都有这个兜底，
            #    bid_analysis 之前遗漏，导致"progress=100% 但 DB 仍是 running"的僵尸。
            #    has_active_task 检查的是内存态 _tasks：finish_task 会 pop 掉，
            #    所以只要 run_inner 里正常调过 finish_task，兜底就不会重复执行。
            try:
                if has_active_task(task_id):
                    logger.warning(
                        "bid_analysis: SSE 生成器退出但任务 %s 仍在内存（run_inner 可能未正常完成），"
                        "兜底标记为 stopped", task_id)
                    try:
                        # ✅ P0 断连修复（2026-09-23）：shield 保证终态落库不被取消打断
                        await asyncio.shield(finish_task(
                            task_id, "stopped", "客户端断开，任务已终止"))
                    except asyncio.CancelledError:
                        logger.info("bid_analysis 断线兜底 finish_task 已 shield（task=%s）",
                                    task_id)
            except Exception:
                logger.exception("bid_analysis: 兜底 finish_task 失败（task=%s）", task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "none",
        },
    )


# =========================================================================
# 5. 核心执行逻辑
# =========================================================================

async def _run_bid_analysis_sync(db, project_id: str, scheme_id: str,
                                  config: AnalysisConfig, tender_text: str,
                                  task_id: str = "",
                                  section_hint: str = "") -> dict:
    """同步执行版本（供 /start 非 SSE 入口使用）。

    task_id 为空字符串时跳过 task_registry（无暂停/停止/进度推送能力）。
    section_hint 非空时作为独立 system 消息注入每次 AI 调用（多标段投标范围）。
    classification_hint 由 classify 能力生成（危大工程分类结论），自动注入提取，
    为空串时与旧版逐字一致。
    """
    # ✅ 2026-09-24：提取前解析方案危大工程分类提示（纯增量，异常已降级为空）
    classification_hint = await _resolve_classification_hint(db, scheme_id)
    # ✅ 2026-09-30：断点续跑 —— 跳过已成功落库的项（force_rerun/单项重跑时忽略）
    task_items = await config.get_task_items_async(db, project_id)
    total = len(task_items)

    if config.force_rerun:
        await _reset_items_for_rerun(
            db, project_id, [it["item_id"] for it in task_items])

    # 预切分
    segments = _split_tender_text(tender_text)  # 单一出口：按配置选分段策略
    logger.info("结构化解析：共 %d 项待执行，原文 %d 字，切分为 %d 段",
                total, len(tender_text), len(segments))

    results = {}
    completed = 0

    # 预热：先单独跑第一个必选项（projectBasicInfo）
    first_item = None
    for it in task_items:
        if it["item_id"] == "projectBasicInfo":
            first_item = it
            break

    # 并发跑其余项
    remaining = [it for it in task_items if it["item_id"] != (first_item or {}).get("item_id")]

    if first_item:
        try:
            res = await _run_single_item(db, project_id, scheme_id,
                                          first_item, tender_text, segments,
                                          section_hint=section_hint)
            results[first_item["item_id"]] = res
            completed += 1
            await _update_item_status(db, project_id, first_item["item_id"],
                                       "success", res.get("content", ""), "",
                                       evidence=_safe_evidence(
                                           res.get("content", ""),
                                           first_item.get("output_type", "markdown"),
                                           tender_text))
            if task_id:
                await update_progress(task_id, completed / total,
                                      message=f"已完成 {completed}/{total}（预热项）")
        except Exception as e:
            logger.exception("预热项执行失败")
            # ✅ BUG 修复（2026-09-23，假成功）：必须同步回写 results —— 当
            #    _run_single_item 已成功、但 _update_item_status 落库失败时，
            #    results 里仍留着「status=success」条目，_get_missing_required
            #    命中 `rid in results` 分支就判为「已完成」，任务最终返回
            #    ok=True；而数据库里该项实际是 error，目录/正文下游读到的是
            #    「缺失」。与下方 _run_item 的异常分支同口径，保证
            #    results 与 DB 状态一致。先写 results 再写 DB：即便这次 DB
            #    写入再次失败，本批结果也已如实反映失败，不会假成功。
            results[first_item["item_id"]] = {"status": "error", "error": str(e)}
            await _update_item_status(db, project_id, first_item["item_id"],
                                       "error", "", str(e))
            completed += 1
            if task_id:
                await update_progress(task_id, completed / total)

        # 等 PROMPT_CACHE_WARMUP_DELAY_MS 让后续并发命中缓存
        # （仅在还有并发项时才有意义；单项重跑不再白等 5 秒）
        if remaining:
            await asyncio.sleep(PROMPT_CACHE_WARMUP_DELAY_MS / 1000)

    semaphore = asyncio.Semaphore(_item_concurrency())  # 外层项并发（可配，默认 2）

    async def _run_item(item: dict):
        """执行单个解析项（暂停闸门与并发许可由 run_with_pause_gate 负责）。"""
        nonlocal completed
        await _update_item_status(db, project_id, item["item_id"], "running", "", "")
        try:
            res = await _run_single_item(db, project_id, scheme_id,
                                         item, tender_text, segments,
                                         section_hint=section_hint,
                                         classification_hint=classification_hint)
            results[item["item_id"]] = res
            await _update_item_status(db, project_id, item["item_id"],
                                       "success", res.get("content", ""), "",
                                       evidence=_safe_evidence(
                                           res.get("content", ""),
                                           item.get("output_type", "markdown"),
                                           tender_text))
        except Exception as e:
            logger.exception("解析项 %s 执行失败", item["item_id"])
            results[item["item_id"]] = {"status": "error", "error": str(e)}
            await _update_item_status(db, project_id, item["item_id"],
                                       "error", "", str(e))
        finally:
            completed += 1
            if task_id:
                await update_progress(task_id, completed / total)

    await asyncio.gather(*[
        run_with_pause_gate(task_id, semaphore, functools.partial(_run_item, it))
        for it in remaining
    ])

    # 必选项校验
    missing = await _get_missing_required(db, project_id, results)

    # ✅ P1（2026-10-04）：非强制重跑同样必须失效下游缓存（此前只有 force_rerun
    #    路径会调 `_invalidate_downstream_cache`）。放在任务终态写入**之前**：
    #    失效是幂等读改写，失败也不得影响「提取已完成」的结论（见函数内注释）。
    await _safe_invalidate_downstream(db, project_id, scheme_id)

    # ✅ BUG 修复（P0）：finish_task 的签名是 (task_id, status, message)，**没有**
    #    progress 参数（全库其余 32 处调用均只传这三个）。此处多传 progress=100
    #    会在**全部解析项跑完之后**的收尾处抛
    #    TypeError: finish_task() got an unexpected keyword argument 'progress'
    #    —— run_inner 捕获后只推 error 事件、永远推不出 completed，
    #    前端表现为「18 项结构化提取未完成 / 解析失败」；且 finish_task 一行都
    #    没执行 → task_registry 残留 running 僵尸、_tasks/_subscribers 不清理。
    #    约定：进度归 update_progress 管，finish_task 只写终态。
    if task_id:
        # ✅ BUG 修复（P0）：任务终态词表必须与全库一致 ——
        #    task_registry / sse_handlers._TERMINAL_STATUSES / 前端
        #    pollTaskUntilTerminal、useSchemeLiveTask、TaskStatusBar 只认
        #    completed / failed / stopped。旧实现写 success / error，
        #    前端永远等不到终态，只能靠「progress=100% 卡住 3 次」的兜底空转
        #    10~20 分钟，任务栏还会显示原始英文 success、done 恒为 false。
        #    注意：这里改的是**任务**终态；bid_analysis_items 的
        #    「解析项」状态仍用 success/error/idle/running（正确，勿改）。
        if missing:
            await finish_task(task_id, status="failed",
                              message=f"必填解析项未完成：{'、'.join(missing)}")
        else:
            # 成功即 100%：显式落一次终态进度（force 绕过节流），
            # 供前端任务栏与启动恢复的「progress 已满」判定使用。
            await update_progress(task_id, 1.0,
                                  message=f"解析完成：{completed}/{total} 项",
                                  force=True)
            await finish_task(task_id, status="completed",
                              message=f"解析完成：{completed}/{total} 项")

    return {
        "ok": len(missing) == 0,
        "completed": completed,
        "total": total,
        "missing_required": missing,
    }


async def _run_bid_analysis_with_progress(db, project_id: str, scheme_id: str,
                                           config: AnalysisConfig, tender_text: str,
                                           progress_callback, task_id: str,
                                           stats_callback=None,
                                           section_hint: str = "") -> dict:
    """带回调进度的版本（供 SSE 入口使用）。

    stats_callback(stats: dict) 可选：切段完成后立即回调一次「提取规模」，
    前端据此展示「共 N 项 × M 段 ≈ K 次模型调用」。为什么必须尽早发：
    超长文档会静默消耗大量 AI 额度（50 万字 = 32 段 × 18 项 = 576 次调用），
    旧实现用户全程不可见，只能等几十分钟后看到账单。

    section_hint 非空时作为独立 system 消息注入每次 AI 调用（多标段投标范围）。
    classification_hint 由 classify 能力生成（危大工程分类结论），自动注入提取，
    为空串时与旧版逐字一致。
    """
    # ✅ 2026-09-24：提取前解析方案危大工程分类提示（纯增量，异常已降级为空）
    classification_hint = await _resolve_classification_hint(db, scheme_id)
    # ✅ 2026-09-30：断点续跑 —— 跳过已成功落库的项（force_rerun/单项重跑时忽略）
    task_items = await config.get_task_items_async(db, project_id)
    total = len(task_items)

    segments = _split_tender_text(tender_text)  # 单一出口：按配置选分段策略
    logger.info("结构化解析（SSE）：共 %d 项待执行，原文 %d 字，切分为 %d 段",
                total, len(tender_text), len(segments))

    if stats_callback:
        stats = {
            "total_chars": len(tender_text),
            "segment_count": len(segments),
            "item_count": total,
            # 每个解析项都要对每段各调一次模型（合并调用除外），
            # 给个量级估算，避免用户不知道自己在烧多少额度。
            "est_model_calls": total * len(segments),
            "chunk_size": DEFAULT_CHUNK_SIZE,
        }
        try:
            res = stats_callback(stats)
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.warning("推送提取规模失败（不影响主流程）: %s", e)

    results = {}
    completed = 0

    first_item = None
    for it in task_items:
        if it["item_id"] == "projectBasicInfo":
            first_item = it
            break

    async def _do_item(item: dict):
        """执行单个解析项（暂停闸门与并发许可由 run_with_pause_gate 负责）。"""
        nonlocal completed
        await _update_item_status(db, project_id, item["item_id"], "running", "", "")
        await progress_callback(item["item_id"], "running", progress=completed / total)

        max_retries = _item_retries()
        last_error = None
        try:
            for attempt in range(max_retries + 1):
                try:
                    res = await _run_single_item(db, project_id, scheme_id,
                                                  item, tender_text, segments,
                                                  section_hint=section_hint,
                                                  classification_hint=classification_hint)
                    results[item["item_id"]] = res
                    content = res.get("content", "")
                    await _update_item_status(db, project_id, item["item_id"],
                                               "success", content, "",
                                               evidence=_safe_evidence(
                                                   content,
                                                   item.get("output_type", "markdown"),
                                                   tender_text))
                    await progress_callback(item["item_id"], "success", content=content,
                                            progress=(completed + 1) / total)
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    logger.warning("解析项 %s 第 %d 次尝试失败: %s",
                                   item["item_id"], attempt + 1, str(e)[:200])
                    # 如果是最后一次尝试，跳出循环写 error
                    if attempt >= max_retries:
                        break
                    # 否则，等一会儿再试（指数退避）
                    wait_s = 5 * (attempt + 1)
                    await asyncio.sleep(wait_s)

            if last_error is not None:
                logger.exception("解析项 %s 执行失败（已重试 %d 次）", item["item_id"], max_retries)
                results[item["item_id"]] = {"status": "error", "error": str(last_error)}
                await _update_item_status(db, project_id, item["item_id"],
                                           "error", "", str(last_error))
                await progress_callback(item["item_id"], "error", error=str(last_error),
                                        progress=(completed + 1) / total)
        finally:
            completed += 1
            await update_progress(task_id, completed / total,
                                  message=f"已完成 {completed}/{total}")

    remaining = [it for it in task_items if it["item_id"] != (first_item or {}).get("item_id")]

    if first_item:
        await run_with_pause_gate(task_id, None, functools.partial(_do_item, first_item))
        # 预热等待只在「后面还有并发项」时才有意义（单项重跑白等 5 秒）
        if remaining:
            await asyncio.sleep(PROMPT_CACHE_WARMUP_DELAY_MS / 1000)

    semaphore = asyncio.Semaphore(_item_concurrency())  # SSE 版项并发（可配，默认 2）
    await asyncio.gather(*[
        run_with_pause_gate(task_id, semaphore, functools.partial(_do_item, it))
        for it in remaining
    ])

    missing = await _get_missing_required(db, project_id, results)

    # ✅ P1（2026-10-04）：与同步入口同口径 —— 非强制重跑也失效下游缓存。
    #    ⚠️ 停止场景同样生效：**已完成的那部分**提取结果已经写库，下游缓存同样
    #    可能陈旧，失效是幂等安全操作，不会把「部分完成」说成「全部完成」。
    await _safe_invalidate_downstream(db, project_id, scheme_id)

    # ✅ 停止语义（2026-09-20 BUG 修复）：is_stopped 必须在 finish_task **之前**
    #    判定 —— finish_task 会把任务从 task_registry._tasks 弹出，此后 is_stopped
    #    对「不存在的任务」一律返回 True，会反过来把「正常跑完」误判成「被停止」。
    stopped = bool(task_id) and is_stopped(task_id)

    # ✅ BUG 修复（P0）：同 _run_bid_analysis_sync —— finish_task 不接受 progress 参数，
    #    传 progress=100 会在全部解析项跑完后抛 TypeError，导致前端永远收不到
    #    completed 事件（表现为「18 项未完成 / 解析失败」）、task_registry 残留
    #    running 僵尸。此处是 SSE 正式入口，正是日志中
    #    「13:34:23 TypeError: finish_task() got an unexpected keyword argument
    #    'progress'」的现场。
    if stopped:
        # 用户主动点「停止」：run_with_pause_gate 会在下一个闸门检查点返回，
        # 未启动的项在 DB 里保持 idle → _get_missing_required 把它们全部列为缺失。
        # 若仍按「缺失即 failed」收尾，用户看到的是「必填解析项未完成：项目级
        # 基本信息、方案级基本信息…」+ 前端弹「提取完成，但必选项未全部完成」，
        # 任务栏显示 failed —— 用户的一次主动停止被报告成失败。
        await update_progress(
            task_id, 1.0,
            message=f"任务已停止：已完成 {completed}/{total} 项",
            force=True)
        await finish_task(
            task_id, status="stopped",
            message=f"任务已停止：已完成 {completed}/{total} 项")
    elif missing:
        await finish_task(task_id, status="failed",
                          message=f"必填解析项未完成：{'、'.join(missing)}")
    else:
        await update_progress(task_id, 1.0,
                              message=f"解析完成：{completed}/{total} 项",
                              force=True)
        await finish_task(task_id, status="completed",
                          message=f"解析完成：{completed}/{total} 项")

    return {"ok": len(missing) == 0 and not stopped, "stopped": stopped,
            "completed": completed, "total": total,
            "missing_required": missing}


async def run_with_pause_gate(task_id: str, semaphore, run_fn):
    """「暂停闸门 → 并发许可 → 执行」的唯一包装入口。

    ✅ 不变量：暂停闸门必须排在获取并发信号量**之前**（与 sse_handlers.guarded_gen
    同源教训）。旧实现先 `async with semaphore` 再在内部 `await wait_resume`，
    暂停期间在途项会把许可全部占住、恢复前任何后续解析项都启动不了。

    task_id 为空（/start 同步入口未注册任务）→ 不设闸门直接执行；
    semaphore 为 None（预热项串行）→ 只过闸门、不限并发。
    """
    if task_id:
        await wait_resume(task_id)
        if is_stopped(task_id):
            return
    if semaphore is None:
        await run_fn()
        return
    async with semaphore:
        # 排队等许可期间可能已被停止 → 复检一次，停止后不再启动新项
        # （暂停闸门仍在信号量之外，暂停不会占住许可）
        if task_id and is_stopped(task_id):
            return
        await run_fn()


async def _run_single_item(db, project_id: str, scheme_id: str,
                            item: dict, tender_text: str, segments: list[str],
                            section_hint: str = "",
                            classification_hint: str = "") -> dict:
    """执行单个解析项。

    分段策略：
      - 单段（len(segments)==1）→ 直接一次 AI 调用
      - 多段 → 逐段提取 → 汇总合并（merge_segmented）
    重试：
      - Markdown 项整体空结果 → 完整重跑一次
      - JSON 项直接返回（collect_json_response 自带修复重试）

    section_hint（多标段投标范围）会同时注入「单项调用」与「分段合并调用」，
    与 OpenBidKit `bidAnalysisTask` 的注入点一一对应（合并阶段同样必须知道
    当前只处理哪个标段，否则会把各标段的片段性表述合并成矛盾结论）。
    """
    item_id = item["item_id"]
    output_type = item.get("output_type", "markdown")

    if len(segments) <= 1:
        result_text = await _run_single_call(item, tender_text, output_type,
                                             section_hint=section_hint,
                                             classification_hint=classification_hint)
    else:
        # 多段并发提取 —— ✅ 加信号量限制（之前直接 gather 所有段，27 段就同时飞 27 个 AI 调用）
        _seg_sem = asyncio.Semaphore(_segment_concurrency())  # 分段并发（可配，默认 3）
        async def _seg_run(seg):
            async with _seg_sem:
                return await _run_single_call(item, seg, output_type,
                                              section_hint=section_hint,
                                              classification_hint=classification_hint)
        partial_results = await asyncio.gather(
            *[_seg_run(seg) for seg in segments],
            return_exceptions=True,
        )
        seg_contents = []
        # ✅ BUG 修复：同步收集「有效的原始分段结果」。旧实现单分段有效时取
        #    partial_results[0] —— 但有效分段未必是第 1 段，且第 1 段可能是
        #    Exception 对象（gather return_exceptions=True）。把 Exception 当
        #    正文落库后，is_missing_result() 对其调 .strip() 直接 AttributeError，
        #    整个解析项报错；即便不是异常，也会把「第 1 段的空结果」顶掉
        #    「其它分段的有效结果」。
        valid_raw: list[str] = []
        for i, pr in enumerate(partial_results):
            if isinstance(pr, Exception):
                # ✅ A-9（2026-10-01）：分段提取是 18 项链路**最高频**的失败点，
                #    旧实现只用 %s 打印异常 message，堆栈全丢，线上无法定位根因
                #    （provider 超时 / 限流 / JSON 解析失败表现完全不同）。
                logger.warning("分段 %d 提取异常: %s", i, pr, exc_info=pr)
                continue
            if pr and not is_missing_result(pr, output_type):
                seg_contents.append(f"--- 分段 {i + 1} ---\n{pr}")
                valid_raw.append(pr)

        if not seg_contents:
            # 全部分段都无有效结果
            if output_type == "markdown":
                result_text = MARKDOWN_MISSING_RESULT
            else:
                # JSON 空结果模板
                import re

                from app.services.bid_analysis_service import _ITEM_PROMPTS
                result_text = re.search(
                    r"```json\s*(\{[^}]+\})",
                    _ITEM_PROMPTS.get(item_id, "{}"), re.DOTALL)
                if result_text:
                    try:
                        obj = json.loads(result_text.group(1))
                        result_text = json.dumps(
                            {k: "没有提及" for k in obj}, ensure_ascii=False, indent=2)
                    except Exception:
                        result_text = "{}"
                else:
                    result_text = "{}"
        elif len(valid_raw) == 1:
            # ✅ 使用唯一有效分段的原始结果（不再错误地取 partial_results[0]）
            result_text = valid_raw[0]
        else:
            # 合并调用
            # ✅ 2026-09-22 增强（对齐 segmentedAiResultMerger.cjs）：
            #    合并消息必须携带原始任务要求（尤其 JSON 项的字段清单），
            #    否则模型不知道要保留哪些 key，合并后字段丢失。
            task_prompt = (get_item_prompt(item_id) or "").replace(
                "__CONTEXT__", "（项目资料已按分段给出，见下方分段解析结果）")
            merge_user = (SEGMENT_MERGE_PROMPT
                          .replace("__TASK_LABEL__", item.get("label", item_id))
                          .replace("__TASK_PROMPT__", task_prompt)
                          .replace("__SEGMENTS__", "\n\n".join(seg_contents)))
            messages = build_system_messages(section_hint, classification_hint) + [
                {"role": "user", "content": merge_user},
            ]
            if output_type == "json":
                result_text = await chat_with_fallback(
                    messages, json_mode=True, timeout=120,
                    scene="bid_analysis_merge")
                try:
                    json.loads(result_text)
                except json.JSONDecodeError:
                    result_text = await _repair_json(result_text, messages,
                                                     scene="bid_analysis_merge")
            else:
                result_text = await chat_with_fallback(
                    messages, timeout=120, scene="bid_analysis_merge")

    # Markdown 项整体空结果 → 完整重跑一次
    if output_type == "markdown" and is_missing_result(result_text, "markdown"):
        logger.info("解析项 %s 首次返回空结果，重跑一次", item_id)
        result_text = await _run_single_call(item, tender_text, "markdown",
                                             section_hint=section_hint,
                                             classification_hint=classification_hint)

    return {"status": "success", "content": result_text, "item_id": item_id}


async def _run_single_call(item: dict, text: str, output_type: str,
                           section_hint: str = "",
                           classification_hint: str = "") -> str:
    """单次 AI 调用：system + user（prompt 模板 + 上下文）。

    section_hint 由 `services/bid_section_context.build_bid_section_context_hint`
    生成（多标段投标范围）；classification_hint 由本模块 classify 能力生成
    （危大工程分类结论，提取重点参考）。两者均为空串时 system 消息与旧版逐字一致。

    ✅ 标段上下文按易标口径作为**独立的第二条 system 消息**下发
    （``buildTenderContextMessages``），不再与通用纪律拼在同一条。
    """
    system_msgs = build_system_messages(section_hint, classification_hint)
    user_msg = {"role": "user", "content": build_item(text, item)}

    if output_type == "json":
        # JSON 项用低温 + JSON 模式
        # ✅ scene：AGENTS.md §4.4 约定「任何新增 AI 调用点必须显式传 scene」，
        #    /ai/stats 按 scene 聚合。本模块此前 5 处调用均未传（审计缺口），
        #    现统一标记为 bid_analysis，便于区分「提取」与目录/正文/事实的调用量。
        response = await chat_with_fallback(
            [*system_msgs, user_msg],
            temperature=0.2,
            json_mode=True,
            timeout=180,
            scene="bid_analysis",
        )
        # 确保是合法 JSON
        try:
            json.loads(response)
        except json.JSONDecodeError:
            # ✅ scene 显式传参（2026-09-23）：这里是**单项提取**的 JSON 修复，
            #    必须归入 bid_analysis 场景，不能沿用 _repair_json 的合并场景默认值，
            #    否则 /ai/stats 按场景聚合时单项提取量会被错记到「分段合并」。
            response = await _repair_json(response, [*system_msgs, user_msg],
                                          scene="bid_analysis")
        return response
    else:
        # Markdown 项
        return await chat_with_fallback(
            [*system_msgs, user_msg],
            temperature=0.3,
            timeout=180,
            scene="bid_analysis",
        )


async def _repair_json(bad_json: str, original_messages: list,
                       scene: str = "bid_analysis_merge") -> str:
    """定向修复非法 JSON（简化版，完整版用 collect_json_response）。

    scene 显式传参（AGENTS.md §4.5：每个 AI 调用点必须标注场景，
    /api/v1/ai/stats 按 scene 聚合）。旧实现硬编码 "bid_analysis_merge"：
    `_run_single_call` 的单项提取修复也会被记到「合并」场景，
    单项提取与分段合并的调用量无法区分。现由调用方显式指定。
    """
    repair_prompt = (
        f"以下 JSON 字符串有语法错误或包含非 JSON 内容，请修复为合法的纯 JSON（"
        f"不要解释、不要 markdown 代码块、不要在 JSON 内部再嵌套 JSON）：\n\n"
        f"```\n{bad_json}\n```"
    )
    response = await chat_with_fallback(
        original_messages + [{"role": "assistant", "content": bad_json},
                             {"role": "user", "content": repair_prompt}],
        json_mode=True, timeout=60, scene=scene)
    try:
        json.loads(response)
        return response
    except json.JSONDecodeError:
        # 最后兜底：尽力从原字符串中提取 JSON
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", bad_json)
        if m:
            try:
                json.loads(m.group())
                return m.group()
            except json.JSONDecodeError:
                pass
        return "{}"


# =========================================================================
# 6. 数据库辅助函数
# =========================================================================

async def _resolve_pid(db, scheme_id: str = "", project_id: str = "") -> str:
    """反查 project_id（与 global_facts._resolve_project_id 对齐）。

    ✅ BUG 修复：旧实现不校验 scheme_id 是否存在 —— 方案不存在时静默返回 ""，
    上层报「需要 scheme_id 或 project_id」，把"参数没传"和"方案不存在"两个
    完全不同的问题混为一谈，用户无从排查。现对齐 global_facts 口径：
    方案不存在 → 404；scheme_id 与 project_id 不匹配 → 400。
    """
    sid = scheme_id.strip() if isinstance(scheme_id, str) else ""
    pid = project_id.strip() if isinstance(project_id, str) else ""
    if pid:
        if sid:
            cur = await db.execute(
                "SELECT project_id FROM schemes WHERE id=?", (sid,))
            row = await cur.fetchone()
            if not row or not row[0]:
                raise HTTPException(404, "方案不存在")
            if str(row[0]) != pid:
                raise HTTPException(400, "scheme_id 与 project_id 不匹配")
        return pid
    if sid:
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (sid,))
        row = await cur.fetchone()
        if not row or not row[0]:
            raise HTTPException(404, "方案不存在")
        return str(row[0])
    return ""


# ✅ 2026-09-25：参与「18 项提取」的字符预算（单一常量）。
# ⚠️ 语义更正（2026-10-01 · A-1）：注释原写「单份文档」，但 _combine_doc_texts
#    的实际实现是 **多文档合计**：`headroom = _MAX_DOC_CHARS - total_chars`，
#    累计达到预算后 `break` —— 后续文档被**整份跳过**（只计入 dropped_doc_count）。
#    这一字之差会让调参者严重高估实际覆盖量，已更正。
# 与 global_facts._extract_markdown_by_type 的入库截断阈值对齐，
# 但语义不同：入库截断作用于 parsed_markdown 本身，本常量作用于
# 多文档聚合时的切片预算 —— 两者必须分开，否则同一份预算被消耗两次。
# 默认值 400000 与 pdf_text_max_pages=500 / MAX_PARSED_CHARS=400000 三级对齐；
# 显式把 bid_analysis_segment_budget 配回 30000 即恢复旧行为。
# ⚠️ 模块级求值（import 期）：与 MAX_PDF_PAGES 同口径，运行期改配置需重启，
#    单测通过 monkeypatch 该常量生效。
_MAX_DOC_CHARS = max(1000, int(getattr(settings, "bid_analysis_segment_budget", None) or 400000))


async def _project_document_cols(db) -> set[str]:
    """读取 ``project_documents`` 的实际列名集合（不缓存：测试会换库）。

    ✅ 幂等迁移友好：``parse_warnings`` / ``parse_truncated`` 均由
    ``db._migrate`` 以「PRAGMA 探测 + 条件 ALTER」增量补列。旧库（迁移前创建、
    或某个只跑部分迁移的临时库）可能缺其中一列。旧实现在整句 SELECT 报
    ``OperationalError`` 时**一刀切回退到基础列**，会把已经存在的
    ``parse_warnings`` 也一并丢掉 —— 新增列越多，降级造成的信息损失越大。
    现按真实列集合动态拼装，缺哪列只丢哪列，其余照常返回。

    :return: 列名集合；表不存在 / 无法探测时返回空集合（调用方按「全缺」处理）。
    """
    try:
        cur = await db.execute("PRAGMA table_info(project_documents)")
        return {dict(r)["name"] for r in await cur.fetchall()}
    except (sqlite3.OperationalError, Exception):
        return set()


async def _bid_analysis_item_cols(db) -> set[str]:
    """读取 ``bid_analysis_items`` 的实际列名集合（不缓存：测试会换库）。

    ✅ 2026-09-30（第十一轮）：``domain`` 列由 ``db._migrate`` 以「PRAGMA 探测
    + 条件 ALTER」增量补列。旧库可能缺该列 —— 写入路径必须先探测再决定是否
    带列 INSERT，否则直接 ``OperationalError`` → 500（本仓 §5.2 同构坑）。
    """
    try:
        cur = await db.execute("PRAGMA table_info(bid_analysis_items)")
        return {dict(r)["name"] for r in await cur.fetchall()}
    except (sqlite3.OperationalError, Exception):
        return set()


async def _list_parsed_documents(db, project_id: str) -> list[dict]:
    """列出项目下所有已解析文档（有 parsed_markdown 且非空）。

    ✅ 2026-09-25：补 SELECT 一列 ``parse_warnings`` —— 该字段此前只存在
    （doc_pipeline 解析阶段写入截断/OCR 降级告警），却从未被任何 SELECT
    读取过，等于"写而不读"。本函数是唯一聚合文档内容的读路径，
    拿到原文后无法判断是否被截断，导致 18 项提取在残缺文本上做提取。

    ✅ 2026-09-26（截断标记贯通）：再补一列 ``parse_truncated`` —— 解析阶段
    已把「字数超限」与「解析器级截断（PDF 截页/表格截行）」持久化到该列，
    本模块此前**始终读不到它**，于是 18 项提取的
    ``_combine_doc_texts(report_truncation=True)`` 的 ``truncated`` 恒为 False，
    前端「项目提取」子页永远看不到"源文档不完整"的告警。两列按
    :func:`_project_document_cols` 的实际列集合动态拼装，缺列只降级该列。
    """
    base = ("SELECT id, file_name, file_type, parsed_markdown, doc_category, "
            "file_size, parse_time")
    where = ("FROM project_documents WHERE project_id=? "
             "AND parsed_markdown IS NOT NULL AND parsed_markdown != '' "
             "ORDER BY created_at, id")
    cols = await _project_document_cols(db)
    diag_cols = [c for c in ("parse_warnings", "parse_truncated") if c in cols]
    sql = base + ((", " + ", ".join(diag_cols)) if diag_cols else "") + " " + where
    try:
        cur = await db.execute(sql, (project_id,))
        return [dict(r) for r in await cur.fetchall()]
    except sqlite3.OperationalError:
        # 极端兜底：PRAGMA 结果与实际表结构不符（外部进程直改库、迁移中断等）
        # → 只选基础列，保证读路径始终可用（诊断信息降级为「未知」）。
        cur = await db.execute(base + " " + where, (project_id,))
        return [dict(r) for r in await cur.fetchall()]


def _doc_text_len(d: dict) -> int:
    """文档正文字符数。

    ✅ 2026-09-25 单一口径：列存在时读库（与 /global-facts/documents 一致）；
    旧库未补列时回退 ``len(parsed_markdown)``，**不会误判为 -1** 把文档当成截断。
    """
    tl = d.get("text_len")
    if isinstance(tl, int):
        return tl
    return len(d.get("parsed_markdown") or "")


#: 解析告警里表示"内容不完整"的关键词（中英都认，兼容历史/外部写入的告警文本）
_TRUNCATION_HINTS: tuple[str, ...] = ("截断", "truncated")


def _doc_is_truncated(d: dict) -> bool:
    """文档正文是否被截断（三层信号取「或」，任一命中即为截断）。

    1. ``parse_truncated`` —— 解析阶段（``parse_document`` /
       ``parse_all_documents``）持久化的权威标记，覆盖「字数超
       ``MAX_PARSED_CHARS``」与「解析器级截断（PDF 截页 / 表格截行）」两类。
    2. 字数达落库上限（当前或历史 80000 字）—— 口径与
       ``global_facts._is_truncated`` 一致。
    3. ``parse_warnings`` 文本兜底 —— 中文告警含「截断」即命中。

    ✅ BUG 修复（2026-09-26，跨模块数据传递断裂）：旧实现只认两条恒为 False
    的路径 —— 「不存在的 ``text_len`` 列 == -1」（``project_documents`` 表从未
    有该列，本函数 SELECT 也不取它）与「``parse_warnings`` 里出现英文
    ``truncated``」（而 :mod:`file_parser` 的告警全是中文，如「原文 X 字，已截断
    至 N 字上限」，绝不含英文）。后果：18 项结构化提取在残缺资料上照常提取，
    且 ``_combine_doc_texts(report_truncation=True)`` 的 ``truncated`` 恒为 False，
    前端与下游都无从得知「提取依据不完整」。

    ⚠️ 三层必须是「或」而不是「第一层命中即返回」：``parse_truncated`` 列
    2026-09-26 才由 ``_migrate`` 补上，**上线前的存量行该列恒为 0**，
    却可能早已在 ``parse_warnings`` 里留下截断告警（解析器与标记位由
    ``_note_truncation`` 同步写入，标记位缺失只可能是补列默认值所致）。
    把它当成「排除条件」会让这批存量文档继续漏报。
    """
    pt = d.get("parse_truncated")
    if isinstance(pt, (int, float)) and not isinstance(pt, bool) and pt:
        return True
    # ② text_len 哨兵值：旧版入库截断写 -1（「入库时被截断」），是既有契约
    #    （tests/test_import_module_fixes_20260925.py::test_doc_is_truncated_*
    #    与 test_doc_text_len_falls_back_without_column 一起钉住的语义）。
    #    新库改由 parse_truncated 列承载该语义，但保留这条判定对旧数据/外部
    #    写入方无害——-1 永远不会出现在未截断的文档上。
    text_len = _doc_text_len(d)
    if text_len == -1:
        return True
    # ③ 字数达落库上限 —— 口径与 global_facts._is_truncated 一致。
    #    注意必须在 ② 之后：_doc_text_len 缺列时回退 len(parsed_markdown)，
    #    回退值恒 >= 0，不会把 -1 误当成截断。
    if text_len > 0:
        # 惰性导入：避免路由模块间的循环导入（global_facts 不反向依赖本模块）
        from app.routers import global_facts as _gf
        if _gf._is_truncated(text_len):
            return True
    # ④ 告警文本兜底（中英都认，兼容历史/外部写入的告警文本）
    warnings = d.get("parse_warnings") or ""
    if not isinstance(warnings, str):
        warnings = " ".join(str(w) for w in warnings)
    return any(k in warnings for k in _TRUNCATION_HINTS)


def _combine_doc_texts(docs: list[dict], report_truncation: bool = False):
    """合并多份已解析文档的文本（优先招标文件，其次合同/设计/地勘）。

    ✅ 2026-09-25 修复：按"已存正文长度"（而非"入库截断长度"）切片。

    旧实现在入库阶段就用 MAX_DOC_CHARS 截断 parsed_markdown，正文本身已残缺；
    这里若再按 len(md) 切片，等于**重复消耗同一份 30000 字预算** ——
    招标文件只有 30000 字余量，第二份 29000 字文档只剩 1000 字被吃掉。
    现读取时按 _MAX_DOC_CHARS 截断，使各文件拿到完整预算；
    入库截断的残缺文档（text_len == -1）原样保留，不在此处再砍。

    :param report_truncation: 为 True 时不返回字符串，而返回
        ``{"text", "total_chars", "truncated", "details"}``，其中 details 是
        每份文档的 ``{name, category, chars, truncated}``（按拼接顺序）。
        默认 False 保持旧调用方的字符串返回，向后兼容。
    """
    if not docs:
        return {"text": "", "total_chars": 0, "truncated": False, "details": []} \
            if report_truncation else ""

    def _sort_key(d: dict):
        return extract_priority(d.get("doc_category"))

    sorted_docs = sorted(docs, key=_sort_key)

    total_chars = 0
    parts = []
    for d in sorted_docs:
        md = d.get("parsed_markdown") or ""
        if not md.strip():
            continue
        cat = d.get("doc_category") or "其他"
        is_truncated = _doc_is_truncated(d)
        # ✅ 按已存正文长度算余量，避免与入库截断叠加导致预算被提前耗尽
        headroom = max(0, _MAX_DOC_CHARS - total_chars)
        if headroom == 0:
            break
        piece = md if len(md) <= headroom else md[:headroom]
        total_chars += len(piece)
        parts.append({
            "name": d.get("file_name", ""),
            "category": cat,
            "text": piece,
            "chars": len(piece),
            "truncated": is_truncated,
        })

    joined = "\n\n---\n\n".join(
        f"# 文档：{p['name']}（分类：{p['category']}）\n\n{p['text']}" for p in parts)
    if not report_truncation:
        return joined
    return {
        "text": joined,
        "total_chars": total_chars,
        # 任一参与提取的文档入库时已被截断，或本函数把某份文档整份跳过，
        # 都意味着"18 项提取看到的不是一份完整资料"，必须显式标记出来。
        "truncated": any(p["truncated"] for p in parts)
        or sum(len(d.get("parsed_markdown") or "")
               for d in docs if (d.get("parsed_markdown") or "").strip()) > total_chars,
        "details": [{"name": p["name"], "category": p["category"],
                     "chars": p["chars"], "truncated": p["truncated"]}
                    for p in parts],
    }


def _combine_doc_texts_report(docs: list[dict]) -> tuple[str, dict]:
    """合并文档文本 + 产出「提取依据完整性」报告（调用方统一入口）。

    ✅ 调用链路补齐（2026-09-26）：``_combine_doc_texts`` 的
    ``report_truncation=True`` 分支自 2026-09-25 加入起**从未被任何调用点
    使用过**（4 处调用全走字符串默认分支）。截断信号已经持久化
    （``project_documents.parse_truncated``）、也能判定（``_doc_is_truncated``），
    却始终停在后端 —— 18 项结构化提取的用户全程无感知「提取依据不完整」。

    本函数把报告收口成一个稳定的小字典，供四个入口（/check-sections、
    /extract-sections、/start、/start-sse）原样透传给前端，避免各处
    自行解释 report 形状而漂移：

    - ``source_truncated``：任一参与提取的文档被截断，或预算不足导致有文档
      整份未被纳入；
    - ``truncated_docs``：被截断的文档清单（沿用 ``_combine_doc_texts`` 的
      ``details`` 形状：name / category / chars / truncated）；
    - ``used_doc_count`` / ``input_doc_count`` / ``dropped_doc_count``：
      纳入提取的文档数 / 项目文档总数 / 未纳入的文档数（预算耗尽被整份
      跳过，或正文为空的文档 —— 两类都意味着用户可能以为资料用上了）；
    - ``total_chars``：实际进入提取的各文档正文片段字数之和
      （不含拼接时插入的文档标题行）。

    :return: ``(合并文本, 报告字典)``。文本语义与旧字符串分支逐字一致。
    """
    report = _combine_doc_texts(docs, report_truncation=True)
    details = report.get("details") or []
    truncated = [d for d in details if d.get("truncated")]
    if truncated or len(details) < len(docs):
        # 提取依据不完整必须留痕：这类问题不报错、静默完成，用户与下游
        # （目录/正文/导出）都无法从失败日志里察觉。
        logger.warning(
            "结构化提取依据不完整：纳入 %d/%d 份文档，被截断 %s，合并文本 %d 字",
            len(details), len(docs),
            "、".join(d.get("name", "?") for d in truncated) or "无",
            int(report.get("total_chars") or 0))
    return report["text"], {
        "source_truncated": bool(report.get("truncated")),
        "truncated_docs": truncated,
        "used_doc_count": len(details),
        "input_doc_count": len(docs),
        "dropped_doc_count": max(0, len(docs) - len(details)),
        "total_chars": int(report.get("total_chars") or 0),
    }


async def _update_item_status(db, project_id: str, item_id: str, status: str,
                              content: str = "", error: str = "",
                              evidence: str | None = None,
                              domain: str = ""):
    """更新单个解析项的状态/内容（upsert）。

    ✅ source 契约：本函数是**AI 写路径**，任何写入都会把 source 复位为 'ai'。
    人工校正走 PUT /results/{item_id}（source='manual'）；若 AI 重跑成功后
    source 仍停在 'manual'，前端会继续显示「已人工校正」徽标，用户就无从分辨
    当前内容到底是 AI 抽的、还是自己改过的 —— 而这恰恰是它要传达的信息。

    ✅ evidence（2026-09-23 来源位置溯源）：仅在调用方显式传入时写入（成功
    checkpoint 传反查结果，可以为空串表示本轮未匹配到出处）；传 None 不动该列
    （running/error 等中间态保留上一轮证据便于对照）。

    ✅ domain（2026-09-30 招标响应域）：主键经 build_item_pk 唯一出口构造。
    ``domain`` 留空时由 :func:`get_item_domain` **按 item_id 自动派生**（默认
    'scheme'）—— 单一事实源，6 处调用点无需逐一传参，杜绝「漏传一处」
    （本仓反复踩的同构陷阱）。scheme 域返回旧主键格式 {project_id}_{item_id}，
    既有查询逐字节不变。旧库缺 domain 列时 INSERT 省略该列，不会 500。
    """
    def_ = get_item_def(item_id) or {}
    resolved_domain = domain or get_item_domain(item_id) or "scheme"
    pk = build_item_pk(project_id, item_id, resolved_domain)
    existing = False
    try:
        cur = await db.execute(
            "SELECT 1 FROM bid_analysis_items WHERE id=?", (pk,))
        existing = await cur.fetchone() is not None
    except Exception:
        existing = False

    if existing:
        fields = ["status=?", "source='ai'", "updated_at=datetime('now','localtime')"]
        params = [status]
        if content:
            fields.append("content=?")
            params.append(content)
        if evidence is not None:
            fields.append("evidence=?")
            params.append(evidence)
        if error:
            fields.append("error=?")
            params.append(error)
        elif status in ("success", "idle"):
            # ✅ 终态成功/重置时清掉上一轮的失败原因：否则「重跑成功后仍显示旧报错」，
            #    前端会把陈旧 error 当本次失败原因展示。
            #    注意 running 不清空 —— 在途项要保留上一轮内容与报错（便于对照）。
            fields.append("error=?")
            params.append("")
        params.append(pk)
        await db.execute(
            f"UPDATE bid_analysis_items SET {', '.join(fields)} WHERE id=?",
            tuple(params))
    else:
        cur_cols = await _bid_analysis_item_cols(db)
        if "domain" in cur_cols:
            await db.execute(
                "INSERT INTO bid_analysis_items "
                "(id, project_id, item_id, label, output_type, required, status, content, error, sort_order, source, evidence, domain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pk, project_id, item_id, def_.get("label", item_id),
                 def_.get("output_type", "markdown"),
                 def_.get("required", 0), status, content, error,
                 def_.get("sort_order", 0), "ai", evidence or "", resolved_domain))
        else:
            await db.execute(
                "INSERT INTO bid_analysis_items "
                "(id, project_id, item_id, label, output_type, required, status, content, error, sort_order, source, evidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (pk, project_id, item_id, def_.get("label", item_id),
                 def_.get("output_type", "markdown"),
                 def_.get("required", 0), status, content, error,
                 def_.get("sort_order", 0), "ai", evidence or ""))
    await db.commit()


async def _invalidate_downstream_cache(db, project_id: str,
                                       scheme_id: str = "") -> int:
    """提取结果变化后失效下游缓存（导出缓存）。

    ✅ 新增（2026-09-22，对齐 OpenBidKit `bidAnalysisTask` 重跑时把
    outlineData / globalFacts / contentGeneration* 一并作废的下游失效语义；
    本软件按既有「缓存失效」机制落地，只清缓存、不删用户数据）：

    提取项（项目级基本信息 / 工程概况 / 施工工艺 …）是目录与正文的输入。
    结果变化后若 `export_cache` 仍命中，导出的 docx 会与最新提取结果不一致
    （界面显示「已重新提取」，导出内容却还是旧的）—— 这是纯粹的静默数据
    不一致，且用户极难自查。失效是幂等安全操作，重复调用无副作用。

    Returns:
        实际失效的方案数（无 scheme_id 时按项目下全部方案处理）。
    """
    sids: list[str] = []
    if scheme_id:
        sids.append(scheme_id)
    elif project_id:
        try:
            cur = await db.execute(
                "SELECT id FROM schemes WHERE project_id=?", (project_id,))
            sids = [str(r[0]) for r in await cur.fetchall() if r[0]]
        except Exception as e:
            logger.warning("查询项目方案失败（跳过缓存失效）: %s", e)
            return 0

    done = 0
    for sid in sids:
        try:
            await invalidate_export_cache(db, sid)
            done += 1
        except Exception as e:
            # 缓存失效失败不应让「提取已完成」的任务变成失败
            logger.warning("方案 %s 导出缓存失效失败（不影响提取结果）: %s", sid, e)
    if done:
        logger.info("提取结果变化：已失效 %d 个方案的导出缓存", done)
    # ✅ 不再受 `done` 门禁（2026-10-01 第七模块专项 · A-3）：
    #    `done==0` 有两种成因 —— ① sids 为空（无 scheme_id 且项目下无方案，
    #    或查询方案失败）；② 逐个 invalidate_export_cache 全部抛错。
    #    两种情况下「提取结果已变化」这一事实依然成立，三处派生产物同样必须作废；
    #    旧实现把它们关在 `if done:` 里，等于第十五轮的级联失效在这两条路径上
    #    完全不生效 → 「已重跑提取，但一致性面板/完整性报告/事实时间戳仍是旧值」
    #    的静默不一致。级联本身幂等且 fail-soft，移出门禁不改变成功路径行为。
    # ✅ 级联补齐（2026-09-30 第十五轮 · 对齐易标重跑时作废下游的语义）：
    #    旧实现**只**清 export_cache，漏掉三处同样以「提取结果」为输入的产物：
    #      ① consistency_scan_cache —— 全文一致性扫描的结论基于旧提取结果，
    #         不清则「刚重跑完提取、一致性面板仍显示旧的冲突清单」；
    #      ② schemes.facts_updated_at —— 目录树据此派生「事实已变更」标记，
    #         不推则用户改了提取项却收不到「章节可能需重写」的提示
    #         （见 AGENTS.md §4.12 第 3 条的读侧派生契约）；
    #      ③ doc_extractions —— 四层存储的提取层快照，清掉后
    #         build_completeness_report 才会按新一轮结果重算覆盖率
    #         （否则「已重新提取」但完整性报告仍是旧数字）。
    #    全部**幂等**（UPDATE/DELETE，重复调用无副作用），且各自 fail-soft：
    #    任一表缺失（测试库/迁移未跑）只记 WARNING，不影响其它。
    await _invalidate_extraction_derived(db, project_id, sids)
    return done


async def _invalidate_extraction_derived(db, project_id: str, sids: list[str]) -> None:
    """提取结果变化后，作废「由提取结果派生」的三处产物（fail-soft、幂等）。"""
    # ① 一致性扫描缓存
    #    ⚠️ 空 sids 守卫（2026-10-01）：`IN ()` 是非法 SQL，靠 except 兜底会
    #    产生一条误导性 WARNING。级联现已对空 sids 也执行（见调用点），故显式跳过。
    if sids:
        try:
            cur = await db.execute(
                "DELETE FROM consistency_scan_cache WHERE scheme_id IN "
                f"({','.join('?' * len(sids))})", tuple(sids))
            from app.db import safe_rowcount
            n = safe_rowcount(cur, what="consistency_scan_cache")
            if n:
                logger.info("提取结果变化：已清 %d 条一致性扫描缓存", n)
        except Exception as e:
            logger.warning("清一致性扫描缓存失败（不影响提取结果）: %s", e)
    # ② 方案级事实时间戳（目录树据此派生「事实已变更」标记）
    if sids:
        try:
            cur = await db.execute(
                "UPDATE schemes SET facts_updated_at=datetime('now','localtime') "
                f"WHERE id IN ({','.join('?' * len(sids))})", tuple(sids))
            from app.db import safe_rowcount
            n = safe_rowcount(cur, what="schemes.facts_updated_at")
            if n:
                logger.info("提取结果变化：已推进 %d 个方案的事实时间戳", n)
        except Exception as e:
            logger.warning("推进事实时间戳失败（不影响提取结果）: %s", e)
    # ③ 四层存储的提取层快照（重算完整性报告的前提）
    #    ⚠️ 只更新 `status`：doc_extractions **没有** updated_at 列
    #    （见 schema_sql.py 的建表语句），带上会整条 SQL 报错、连带这一轮失效全废。
    #    'stale' 正是 §4.11.5 已确立的「物化陈旧」状态值，口径一致。
    try:
        cur = await db.execute(
            "UPDATE doc_extractions SET status='stale' WHERE project_id=?",
            (project_id,))
        from app.db import safe_rowcount
        n = safe_rowcount(cur, what="doc_extractions")
        if n:
            logger.info("提取结果变化：已把 %d 条提取层快照标记为 stale", n)
        await db.commit()
    except Exception as e:
        logger.warning("作废提取层快照失败（不影响提取结果）: %s", e)


async def _safe_invalidate_downstream(db, project_id: str, scheme_id: str = "") -> None:
    """**收尾专用**的下游缓存失效包装（fail-soft 到极致，绝不改变任务终态）。

    ✅ BUG 修复（P1 · 2026-10-04）：`_invalidate_downstream_cache` 的 docstring
    自称「提取结果变化后失效下游缓存」，但它**只在 force_rerun 的路径上被调用**
    （`_reset_items_for_rerun`，见 :func:`start_bid_analysis` 与 SSE 入口的
    `if force_rerun:` 分支）。而真实的高频路径恰恰是**非强制重跑**：用户在
    BidAnalysisTab 点「重新提取（全部/单项）」时若不勾 force，任务照样跑完 18 项
    并把新结果写进 `bid_analysis_items`，收尾分支却一行失效都不做 →
    `export_cache` / `consistency_scan_cache` / `schemes.facts_updated_at` /
    `doc_extractions` 全部停留在旧值：
      · 导出 docx 用的是上一轮提取结果（界面显示「已重新提取」，导出内容却是旧的）；
      · 一致性面板仍显示旧冲突清单；
      · 目录树收不到「事实已变更」提示。
    这是纯粹的静默数据不一致，且用户极难自查。

    现在两条收尾路径（同步 / SSE）的**任务终态写入之前**统一调用本函数。
    为什么再包一层 try：`_invalidate_downstream_cache` 内部虽已逐表 fail-soft，
    但它是「提取已完成」之后的新增动作，任何未预料的异常都不应让一个已成功的
    任务变成异常（与 `_get_missing_required` 之类收尾动作的容错口径一致）。
    """
    try:
        await _invalidate_downstream_cache(db, project_id, scheme_id)
    except Exception as e:  # noqa: BLE001 - 收尾动作不得改变任务终态
        logger.warning("提取收尾：下游缓存失效失败（不影响提取结果）: %s", e)


async def _reset_items_for_rerun(db, project_id: str,
                                 item_ids: list[str] | None = None):
    """force_rerun 时把「本次将执行」的解析项重置为 idle（并在库中补齐记录）。

    ✅ 语义收敛（修复数据丢失）：旧实现无条件重置该项目**全部**解析项，
    而自定义模式（含前端「单项重新提取」按钮）只跑其中一部分 —— 未被选中的项
    会被清空且本轮不会重跑，用户已提取的结果被静默删除。
    现按 item_ids 收敛：None / 空列表 = 全部（兼容 key/full 全量重跑语义）。
    """
    try:
        from app.services.bid_analysis_service import ANALYSIS_ITEMS
        if item_ids:
            defs = [d for d in (get_item_def(i) for i in item_ids) if d]
        else:
            defs = list(ANALYSIS_ITEMS)
        if not defs:
            return

        # 1) 先确保本次要跑的解析项都在 DB 中有记录（upsert）
        for def_ in defs:
            pk = f"{project_id}_{def_['item_id']}"
            try:
                cur = await db.execute(
                    "SELECT 1 FROM bid_analysis_items WHERE id=?", (pk,))
                exists = await cur.fetchone() is not None
            except Exception:
                exists = False
            if not exists:
                await db.execute(
                    "INSERT INTO bid_analysis_items "
                    "(id, project_id, item_id, label, output_type, required, status, content, error, sort_order, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pk, project_id, def_["item_id"], def_.get("label", def_["item_id"]),
                     def_.get("output_type", "markdown"),
                     def_.get("required", 0), "idle", "", "",
                     def_.get("sort_order", 0), "ai"))

        # 2) 重置为 idle（仅本次要执行的项）；source 一并复位为 'ai'
        ids = [d["item_id"] for d in defs]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE bid_analysis_items SET status='idle', content='', error='', "
            f"source='ai', evidence='' "
            f"WHERE project_id=? AND item_id IN ({placeholders})",
            (project_id, *ids))
        await db.commit()
        logger.info("重置项目 %s 的 %d 个解析项为空（force_rerun）",
                    project_id, len(ids))
        # ✅ 提取结果已作废 → 下游（导出缓存）必须同步失效，否则导出内容与
        #    最新提取结果不一致（见 _invalidate_downstream_cache 说明）。
        await _invalidate_downstream_cache(db, project_id)
    except Exception as e:
        logger.warning("重置解析项失败: %s", e)


async def clear_interrupted_items(db, project_id: str = "") -> int:
    """把仍停留在 running 的解析项标记为「中断」，避免永久卡在「运行中」。

    为什么需要：`_update_item_status(..., "running")` 与写终态之间存在窗口，
    以下路径都可能在此窗口内退出而**不写终态**：
      - 进程重启（uvicorn --reload / 手动重启）：内存任务直接消失；
      - 客户端断开 / 用户点「停止」：`event_generator` 的 finally 会
        `task.cancel()`，CancelledError 属 BaseException，**不会**被
        `_do_item` 的 `except Exception` 捕获 → 不写终态。
    后果：该项永久 running —— UI 永远显示「运行中」、summary.running>0、
    Tab 徽标永远不是绿色、必选项永远判为缺失（表现为「18 项未完成」）。

    调用时机（三处，覆盖所有中断场景）：
      1) 新一轮解析启动前（清理上一轮残留）；
      2) SSE 运行体异常/取消时（清理本轮在途项）；
      3) 后端启动恢复（main.py，清理进程被杀留下的残留）。

    project_id 为空 → 清理全部项目（仅供启动恢复使用）。
    """
    try:
        where = "status='running'"
        params: tuple = ()
        if project_id:
            where = "project_id=? AND status='running'"
            params = (project_id,)
        cur = await db.execute(
            "UPDATE bid_analysis_items SET status='error', "
            "error='上次执行被中断（进程重启或任务取消），请重新提取', "
            f"updated_at=datetime('now','localtime') WHERE {where}", params)
        # ✅ P1 修复（2026-09-29 · R13 漏改点）：AGENTS.md §5.5 —— 全局单写连接 +
        #    aiosqlite 下 execute() **可能返回 None**，旧实现 `cur.rowcount` 直接
        #    AttributeError，又被下方 `except Exception` 吞掉 → 中断遗留的 running
        #    解析项**静默残留**（UI 永远显示「运行中」、summary.running>0、18 项
        #    永久判缺失、Tab 徽标永远不绿）。现统一走 safe_rowcount 并在 None 时
        #    打**带处置建议**的告警：既不再崩，也不再让调用方误以为清理成功。
        await db.commit()
        n = safe_rowcount(cur, what="中断遗留 running 解析项置 error")
        if n:
            logger.warning("清理了 %d 个中断遗留的 running 解析项（project=%s）",
                           n, project_id or "<全部>")
        return n
    except Exception as e:
        # exc_info=True：这是启动恢复/任务收尾的关键路径，失败原因必须能定位，
        # 只打 %s 会丢失堆栈（连接损坏 vs SQL 语法错误 vs 锁竞争，处置完全不同）。
        logger.warning("清理中断解析项失败（中断项可能残留为 running，"
                       "需重试提取或重启后端）: %s", e, exc_info=True)
        return 0


async def _get_missing_required(db, project_id: str, results: dict) -> list[str]:
    """检查必选项（REQUIRED_ITEM_IDS）是否都 success 且有有效内容，返回缺失的中文标签列表。

    「有效内容」统一走 is_missing_result —— markdown 的「未提取到」、json 的全
    「没有提及」都算缺失，与 /results 汇总、format_downstream_context 同口径。
    """
    missing = []
    for rid in REQUIRED_ITEM_IDS:
        def_ = get_item_def(rid) or {}
        item_label = def_.get("label", rid)
        if rid in results:
            r = results[rid]
            if r.get("status") != "success" or not r.get("content"):
                missing.append(item_label)
            elif is_missing_result(r.get("content", ""), def_.get("output_type", "markdown")):
                missing.append(item_label)
        else:
            # 检查数据库中是否有
            try:
                cur = await db.execute(
                    "SELECT status, content FROM bid_analysis_items "
                    "WHERE project_id=? AND item_id=?",
                    (project_id, rid))
                row = await cur.fetchone()
                if (not row or row["status"] != "success"
                        or not row["content"]
                        or is_missing_result(row["content"],
                                             def_.get("output_type", "markdown"))):
                    missing.append(item_label)
            except Exception:
                missing.append(item_label)
    return missing


# =========================================================================
# 危大工程分类与九大章节字段完整性（2026-09-24 新增）
# =========================================================================
async def _load_extraction_map(db, project_id: str, scheme_id: str) -> dict:
    """读取该方案/项目的 18 项提取结果，组装为 {item_id: content}。

    口径与 format_downstream_context / _get_missing_required 一致：
    优先精确匹配 scheme_id，取不到再退回项目级（scheme_id=''）。
    """
    rows: list[dict] = []
    if scheme_id:
        cur = await db.execute(
            "SELECT item_id, content, status, output_type FROM bid_analysis_items "
            "WHERE project_id=? AND scheme_id=?",
            (project_id, scheme_id))
        rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        cur = await db.execute(
            "SELECT item_id, content, status, output_type FROM bid_analysis_items "
            "WHERE project_id=? AND (scheme_id='' OR scheme_id IS NULL)",
            (project_id,))
        rows = [dict(r) for r in await cur.fetchall()]
    out: dict = {}
    for r in rows:
        if r.get("status") == "success" and (r.get("content") or "").strip():
            out[r["item_id"]] = r["content"]
    return out


async def _fetch_scheme_name(db, scheme_id: str) -> str:
    if not scheme_id:
        return ""
    try:
        cur = await db.execute("SELECT name FROM schemes WHERE id=?", (scheme_id,))
        row = await cur.fetchone()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


async def _resolve_classification_hint(db, scheme_id: str) -> str:
    """解析本方案危大工程分类提示串，供提取循环注入 system 消息。

    优先级：
      1. settings.scheme_auto_classify 关闭 → 返回空（不注入，保持旧行为）；
      2. schemes 已存 scheme_classification_json（曾显式调用 /classify）→ 直接复用，
         保证「分类结果」在提取阶段被消费（数据传递闭环）；
      3. 仅有方案名称 → 按名称关键词实时分类（纯函数，零额外 AI 成本）；
      4. 取不到名称/无分类 → 空串。

    ✅ 纯增量：返回空串时与旧版 system 消息逐字一致；任何异常均降级为空（不阻断提取）。
    """
    if not settings.scheme_auto_classify or not scheme_id:
        return ""
    try:
        cur = await db.execute(
            "SELECT name, scheme_classification_json FROM schemes WHERE id=?", (scheme_id,))
        row = await cur.fetchone()
        if not row:
            return ""
        name, stored = row[0] or "", row[1] or ""
        if stored:
            try:
                data = json.loads(stored)
                return sc.format_classification_hint(sc.SchemeClassification(**data))
            except Exception:
                pass
        if name:
            return sc.build_classification_hint(name)
    except Exception as e:  # 列缺失/解析失败都不阻断提取
        logger.warning("解析方案分类提示失败（不阻断提取）: %s", e)
    return ""


@router.post("/classify")
async def classify_scheme_api(
    body: dict,
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    db=Depends(get_db),
):
    """危大工程方案自动分类 + 九大章节字段完整性校验。

    流程：
      1. 反查 project_id（scheme_id 不存在 → 404）；
      2. 取方案名称（body.scheme_name 优先，否则读 schemes.name）；
      3. 结合 body.params（阈值判定参数：depth/height/span/...）与 body.extra_text
         （资料补充文本）做方案名称关键词解析 + 阈值判定；
      4. 读取 18 项提取结果，做九大章节字段完整性差集分析；
      5. 写入 schemes 分类列（hazard_category/hazard_subcategory/is_hazardous/
         is_oversize/scheme_classification_json）；旧库无这些列时优雅降级不写；
      6. 返回分类结果与字段完整性。

    ✅ 纯增量：仅在显式调用时触发，不改既有 18 项提取字段；settings.scheme_auto_classify
       为 False 时返回 disabled（与「新增能力默认可关闭」一致）。
    """
    if not settings.scheme_auto_classify:
        return {"ok": False, "disabled": True,
                "reason": "scheme_auto_classify 已关闭，未执行分类"}

    real_pid = await _resolve_pid(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    scheme_name = (body.get("scheme_name") or "").strip() \
        or await _fetch_scheme_name(db, scheme_id)
    params = body.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    extra_text = (body.get("extra_text") or "").strip()

    classification = sc.classify_scheme(scheme_name, params, extra_text)
    primary_category = classification.category_ids[0] if classification.category_ids else None

    extraction = await _load_extraction_map(db, real_pid, scheme_id)
    completeness = sc.validate_chapter_fields(extraction, primary_category)

    # 写入 schemes 分类列（幂等；旧库缺列时跳过，不阻断返回）
    if scheme_id:
        try:
            cur = await db.execute("PRAGMA table_info(schemes)")
            cols = {r[1] for r in await cur.fetchall()}
            if "scheme_classification_json" in cols:
                await db.execute(
                    "UPDATE schemes SET hazard_category=?, hazard_subcategory=?, "
                    "is_hazardous=?, is_oversize=?, scheme_classification_json=?, "
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    (",".join(classification.category_ids),
                     ",".join(classification.sub_ids),
                     1 if classification.is_hazardous else 0,
                     1 if classification.is_oversize else 0,
                     json.dumps(classification.to_dict(), ensure_ascii=False),
                     scheme_id))
                await db.commit()
        except Exception as e:  # 写库失败不阻断分类结果返回
            logger.warning("写入方案分类失败（不影响返回）: %s", e)

    return {
        "ok": True,
        "scheme_id": scheme_id,
        "project_id": real_pid,
        "classification": classification.to_dict(),
        "primary_category": primary_category,
        "chapter_completeness": completeness,
    }
