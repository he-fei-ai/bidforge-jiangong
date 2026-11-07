"""分区混合生成（R179 Phase F · 2026-11-06）。

按目录节点的 ``content_type`` 把正文分成两类策略：

* **规范驱动（rag）**：``standard`` / ``calculation`` / ``verification`` ——
  先检索条款，生成器只做语境化编织，数值受 :mod:`app.services.numeric_lock`
  机械约束；
* **经验驱动（llm）**：``experience`` / ``schedule`` —— 由 LLM 主导，但必须引用
  项目事实值，且禁止无信息量表述（黑名单可配置）。

边界
----
* 条款检索**只复用** E 阶段的唯一实现（``GET /api/v1/clause-library/search``
  的后端实现 ``clause_store.search_clauses``），本模块不新增检索、不直接查
  ``standard_clauses``；
* 本模块不新增任何 LLM 调用：只负责把条款上下文 / 数值锁清单 / 经验段约束
  写进既有生成链路的提示词，生成仍走既有可注入 ``chat_with_fallback`` 通道；
* 纯函数优先（路由、上下文拼装、经验段校验），检索函数以 ``search_fn`` 注入，
  便于单测替换为假实现，绝不依赖外部服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field

from app.config import settings
from app.services import clause_candidates, clause_store
from app.services.numeric_lock import (
    AllowedNumbers,
    NumericLockResult,
    ScopedAllowedNumbers,
    build_scoped_allowed_numbers,
    check_text,
    extract_numbers,
)
from app.services.outline_templates import (
    CONTENT_TYPES,
    FACT_DEP_ALIASES,
    content_type_of,
    fact_deps_of,
)

#: 规范驱动型内容类型（RAG 主导）。
RAG_CONTENT_TYPES: tuple[str, ...] = ("standard", "calculation", "verification")

#: 经验驱动型内容类型（LLM 主导）。
LLM_CONTENT_TYPES: tuple[str, ...] = ("experience", "schedule")

logger = logging.getLogger("hybrid")

#: 经验驱动段落默认黑名单：命中即计缺陷（可由 ``hybrid_vague_blacklist`` 覆盖）。
DEFAULT_VAGUE_PHRASES: tuple[str, ...] = (
    "根据实际情况确定",
    "按实际情况确定",
    "按规范施工",
    "按规范要求施工",
    "按设计要求施工",
    "按设计确定",
    "具体情况具体分析",
    "视现场情况而定",
    "由施工单位自行确定",
    "按常规做法",
)

#: 「事实缺失」显式标注关键词（生成侧与校验侧共用，避免两处措辞分叉）。
MISSING_NOTICE_MARKER = "待确认"


@dataclass(frozen=True)
class SectionRoute:
    """单章节的路由决策（生成策略 + 事实状态）。"""

    title: str
    content_type: str
    strategy: str
    fact_status: str = "unknown"
    fact_values: dict[str, str] = field(default_factory=dict)
    missing_deps: tuple[str, ...] = ()
    meta_source: str = "title"


def _node_meta(node: dict) -> dict:
    """节点元数据：优先节点自带键，其次解析 ``sections.outline_json``。"""
    if isinstance(node.get("content_type"), str) and node.get("content_type") in CONTENT_TYPES:
        return node
    raw = node.get("outline_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def strategy_for(content_type: str) -> str:
    """内容类型 → 生成策略（rag / llm）。"""
    return "rag" if content_type in RAG_CONTENT_TYPES else "llm"


def route_node(node: dict) -> SectionRoute:
    """按节点元数据给出路由；元数据缺失时按标题确定性派生。"""
    node = node if isinstance(node, dict) else {}
    title = str(node.get("title") or "")
    meta = _node_meta(node)
    content_type = meta.get("content_type")
    meta_source = "outline_json"
    if not (isinstance(content_type, str) and content_type in CONTENT_TYPES):
        content_type = content_type_of(title)
        meta_source = "title"

    fact_status = str(meta.get("fact_status") or "unknown")
    fact_values: dict[str, str] = {}
    raw_values = meta.get("fact_values")
    if isinstance(raw_values, dict):
        for dep, item in raw_values.items():
            value = str(item.get("value") or "") if isinstance(item, dict) else str(item or "")
            if value:
                fact_values[str(dep)] = value

    missing: list[str] = []
    for dep in meta.get("missing_fact_deps") or ():
        dep = str(dep or "")
        if dep and dep not in missing:
            missing.append(dep)
    if not missing and fact_status in ("missing", "partial"):
        for dep in meta.get("fact_deps") or ():
            dep = str(dep or "")
            if dep and dep not in missing:
                missing.append(dep)

    return SectionRoute(
        title=title,
        content_type=content_type,
        strategy=strategy_for(content_type),
        fact_status=fact_status,
        fact_values=fact_values,
        missing_deps=tuple(missing),
        meta_source=meta_source,
    )


#: 单节检索默认超时（秒）与单方案总检索次数上限（可由 config 覆盖）。
_DEFAULT_SEARCH_TIMEOUT = 3.0
_DEFAULT_SEARCH_BUDGET = 500

#: 进程内结果复用上界：命中与空结果都缓存，但绝不做无界内存表。
_SEARCH_CACHE_MAX = 256
_SEARCH_CACHE: "OrderedDict[tuple, clause_store.ClauseSearchResult]" = OrderedDict()

#: 原因码 → 「因此跳过了什么校验」（报告与提示词共用，避免两处措辞分叉）。
_SKIP_NOTES: dict[str, str] = {
    "no_corpus": "条款库为空或 standard_clauses 表缺失，未做条款符合性校验；数值锁按 undetermined 处理",
    "no_standard_code": (
        "无法从文种收敛出候选标准号，未做条款符合性校验" "（不退回全库标题模糊匹配）；数值锁按 undetermined 处理"
    ),
    "no_match": "候选标准范围内无匹配条款，未做条款符合性校验；数值锁按 undetermined 处理",
    "error": "条款检索异常、超时或预算超限，未做条款符合性校验；数值锁按 undetermined 处理",
}

_CJK_ONLY_RE = re.compile(r"[\u4e00-\u9fff]")


def reset_clause_search_cache() -> None:
    """清空进程内检索缓存（测试隔离用；生产由每次生成自然累积并受上界约束）。"""
    _SEARCH_CACHE.clear()


def clause_search_cache_size() -> int:
    """当前缓存条目数（用于断言「空结果缓存有上界」）。"""
    return len(_SEARCH_CACHE)


@dataclass
class ClauseSearchBudget:
    """单方案检索预算：次数上限 + 超限计数（超限必须进报告，不静默丢弃）。"""

    limit: int = _DEFAULT_SEARCH_BUDGET
    used: int = 0
    skipped: int = 0

    def consume(self) -> bool:
        if self.limit > 0 and self.used >= self.limit:
            self.skipped += 1
            return False
        self.used += 1
        return True

    def as_dict(self) -> dict:
        return {
            "limit": self.limit,
            "used": self.used,
            "skipped": self.skipped,
            "exhausted": self.skipped > 0,
        }


def search_skip_note(status: str) -> str:
    """原因码 → 跳过校验说明。"""
    return _SKIP_NOTES.get(status, "检索未成功，未做条款符合性校验；数值锁按 undetermined 处理")


def search_report_fields(
    result: clause_store.ClauseSearchResult,
    *,
    budget: ClauseSearchBudget | None = None,
) -> dict:
    """三态检索结果 → hybrid_report 字段（「本节未经规范校验」由此强制落地）。"""
    verified = result.status == "ok" and bool(result.hits)
    note = "" if verified else search_skip_note(result.status)
    return {
        "search_status": result.status,
        "search_reason_code": result.reason_code or result.status,
        "standard_candidates": list(result.candidates),
        "query_terms": list(result.terms),
        "skipped_checks": note,
        "section_unverified": not verified,
        "section_unverified_note": "" if verified else f"本节未经规范校验：{note}",
        "budget_exceeded": bool(result.budget_exceeded),
        "budget": budget.as_dict() if budget is not None else None,
    }


def _fact_query_terms(title: str) -> tuple[str, ...]:
    """标题事实依赖 → 中文别名词项（ASCII 别名易与单位/字段名混淆，过滤掉）。"""
    terms: list[str] = []
    for dep in fact_deps_of(title):
        for alias in FACT_DEP_ALIASES.get(dep, ()):
            if len(alias) >= 2 and _CJK_ONLY_RE.search(alias) and alias not in terms:
                terms.append(alias)
    return tuple(terms[:4])


async def search_for_section(
    db,
    title: str,
    *,
    content_type: str = "",
    category_ids=(),
    template_key: str = "",
    limit: int = 8,
    search_fn=None,
    budget: ClauseSearchBudget | None = None,
    timeout: float | None = None,
) -> clause_store.ClauseSearchResult:
    """检索与章节内容相关的条款（文种收敛 → 候选内词项匹配）。

    与旧实现的区别：**不再拿章节标题直接查全库**。先由 ``category_ids`` /
    ``template_key`` 收敛出候选标准号基号，再在候选范围内做条文号/词项匹配；
    候选集为空直接落 ``no_standard_code``，绝不退回全库模糊匹配。

    ``search_fn`` 可注入（测试用假实现），签名与
    :func:`app.services.clause_store.search_clauses_in_candidates` 一致。

    返回的 ``status`` 是原因码（``no_corpus`` / ``no_standard_code`` /
    ``no_match`` / ``error``），调用点必须据此记 ``undetermined`` 并写进报告，
    不能把「没检查」显示成「没问题」。
    """
    fn = search_fn or clause_store.search_clauses_in_candidates
    candidates = clause_candidates.candidate_standard_bases(category_ids, template_key)
    if not candidates:
        return clause_store.ClauseSearchResult(
            status="no_standard_code",
            reason_code="no_standard_code",
            message="无法判定：未能从文种收敛出候选标准号",
        )

    extra_terms = _fact_query_terms(title)
    cache_key = (candidates, str(title), str(content_type), extra_terms, int(limit))
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        _SEARCH_CACHE.move_to_end(cache_key)
        return cached

    if budget is not None and not budget.consume():
        return clause_store.ClauseSearchResult(
            status="error",
            reason_code="error",
            message=f"检索预算超限（本节未检索；已跳过 {budget.skipped} 节）",
            candidates=candidates,
            budget_exceeded=True,
        )

    wait_seconds = timeout
    if wait_seconds is None:
        wait_seconds = float(
            getattr(settings, "hybrid_clause_search_timeout_seconds", _DEFAULT_SEARCH_TIMEOUT)
            or _DEFAULT_SEARCH_TIMEOUT
        )
    try:
        result = await asyncio.wait_for(
            fn(
                db,
                standard_bases=candidates,
                query=title,
                content_type=str(content_type),
                extra_terms=extra_terms,
                limit=int(limit),
            ),
            timeout=max(0.001, wait_seconds),
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("条款检索超时（章节 %s，%.2fs）", title, wait_seconds)
        return clause_store.ClauseSearchResult(
            status="error",
            reason_code="error",
            message=f"无法判定：条款检索超时（{wait_seconds:.2f}s）",
            candidates=candidates,
        )
    except Exception as exc:  # noqa: BLE001 - 任何检索失败都要变成可见的三态结果
        logger.warning("条款检索失败（章节 %s）：%s", title, exc, exc_info=True)
        return clause_store.ClauseSearchResult(
            status="error",
            reason_code="error",
            message=f"无法判定：条款库检索异常（{exc}）",
            candidates=candidates,
        )

    if result.status in ("ok", "no_match"):
        _SEARCH_CACHE[cache_key] = result
        while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.popitem(last=False)
    return result


def _clause_label(hit: clause_store.ClauseRef) -> str:
    clause_no = str(hit.clause_no or "").strip()
    # 库内 clause_no 可能已带「第/条」，只在缺失时补，避免「第第5.2.1条条」。
    if clause_no and not clause_no.startswith("第"):
        clause_no = f"第{clause_no}条"
    return f"《{hit.standard_name or hit.standard_code}》{hit.standard_code} {hit.version} {clause_no}".strip()


def clause_context_block(result: clause_store.ClauseSearchResult) -> str:
    """条款上下文块：命中逐条列出摘录与来源；未命中/无语料显式「无法判定」。"""
    if result.status != "ok" or not result.hits:
        message = (result.message or "未检索到相关条款").strip()
        for prefix in ("无法判定：", "无法判定:"):
            if message.startswith(prefix):
                message = message[len(prefix) :].strip()
        return (
            "【条款检索结果】无法判定："
            + message
            + "。\n【未经规范校验】本节未经规范校验（"
            + search_skip_note(result.status)
            + "）。不得据此编造条文号、数值、阈值或强制性表述。"
        )
    lines = ["【条款检索上下文】（来源：GET /api/v1/clause-library/search，仅摘录要点）"]
    for hit in result.hits:
        source = str(hit.source_url or "").strip() or (
            f"页码 {hit.page_ref}" if str(hit.page_ref or "").strip() else "来源未登记"
        )
        lines.append(f"- {_clause_label(hit)}（匹配方式：{hit.match_type}）：{hit.excerpt}")
        lines.append(f"  来源：{source}；条款标识：{hit.clause_uid}")
    return "\n".join(lines)


def numeric_lock_block(allowed: AllowedNumbers | ScopedAllowedNumbers) -> str:
    """数值锁清单块：把允许集合显式告知模型，同时声明程序会机械拦截。"""
    if isinstance(allowed, ScopedAllowedNumbers):
        rows = [(label, display) for label, display in allowed.display_groups() if display]
        if not rows:
            return (
                "【数值锁（程序强制校验）】本章未检索到可引用的条款数值，也未取得项目事实值："
                "正文不得写入任何具体数值（阈值/尺寸/参数），只写定性控制要求与取值来源。"
            )
        lines = [
            "【数值锁（程序强制校验）】逐条条款绑定：一个句子只能引用它明确标注的那一条条款的数值，"
            "禁止跨条款拼数（项目事实值可用于任意句子）。"
        ]
        lines.append("可写出的数值（按条款分组）：")
        for label, display in rows:
            lines.append(f"- {label}：{display}")
        lines.append(
            "引用条款数值时必须在本句写明该条款号（如「第5.2.1条」）；"
            "清单外、或与该句所引条款不匹配的数值一律不得写入。"
        )
        return "\n".join(lines)
    if not allowed.tokens:
        return (
            "【数值锁（程序强制校验）】本章未检索到可引用的条款数值，也未取得项目事实值："
            "正文不得写入任何具体数值（阈值/尺寸/参数），只写定性控制要求与取值来源。"
        )
    return (
        "【数值锁（程序强制校验）】本章可写出的数值仅限："
        + allowed.display()
        + "。清单外的数值一律不得写入；确需引用时写「取值经专项计算书核定」，不得自拟数值。"
    )


def fact_notice_block(route: SectionRoute) -> str:
    """事实缺失块：显式标注缺失项，禁止编造数值，也禁止悄悄留空。"""
    if route.fact_status in ("missing", "partial") and route.missing_deps:
        deps = "、".join(route.missing_deps)
        return (
            f"【事实缺失】以下依赖项未取得已确认的项目事实：{deps}。"
            f"正文必须逐项以「{MISSING_NOTICE_MARKER}（缺少{deps}）」明确标注，"
            "不得编造数值填上，也不得略过不写。"
        )
    if route.fact_status == "missing":
        return (
            "【事实缺失】本章未取得任何已确认的项目事实：正文必须以"
            f"「{MISSING_NOTICE_MARKER}」标注缺失项，不得编造数值填上，也不得略过不写。"
        )
    return ""


def experience_block(route: SectionRoute, blacklist: tuple[str, ...]) -> str:
    """经验驱动段落约束块：必须引用项目事实值 + 禁止无信息量表述。"""
    lines = ["【经验驱动段落约束】"]
    if route.fact_values:
        lines.append("1. 必须引用下列项目事实中的具体数据（正文至少出现其中一处）：")
        for dep, value in route.fact_values.items():
            lines.append(f"- {dep}：{value}")
    else:
        lines.append(f"1. 本章无可用项目事实：涉及具体数据处一律写「{MISSING_NOTICE_MARKER}」，不得自拟。")
    lines.append("2. 禁止无信息量表述：" + "、".join(blacklist) + "（出现即计缺陷）。")
    return "\n".join(lines)


def build_hybrid_block(
    route: SectionRoute,
    result: clause_store.ClauseSearchResult,
    allowed: AllowedNumbers,
    *,
    blacklist: tuple[str, ...] = DEFAULT_VAGUE_PHRASES,
) -> str:
    """拼装注入生成提示词的完整混合生成块（按策略分流）。"""
    header = (
        f"【分区生成策略（程序判定）】本章内容类型：{route.content_type}；"
        f"生成策略：{'规范驱动（RAG 主导）' if route.strategy == 'rag' else '经验驱动（LLM 主导）'}。"
    )
    parts = [header]
    if route.strategy == "rag":
        parts.append(
            "本章以检索到的条款为骨架，只做语境化编织（工序衔接、现场表述与执行细节），"
            "不得改写条款的数值、阈值、适用条件与强制性条文措辞。"
        )
        parts.append(clause_context_block(result))
        parts.append(numeric_lock_block(allowed))
    else:
        parts.append(experience_block(route, blacklist))
    notice = fact_notice_block(route)
    if notice:
        parts.append(notice)
    return "\n\n".join(parts)


def _clause_aliases(hit: clause_store.ClauseRef) -> tuple[str, ...]:
    """句子引用识别别名：条款标识 / 条款号（含「第…条」写法）。"""
    aliases: list[str] = []
    uid = str(hit.clause_uid or "").strip()
    if uid:
        aliases.append(uid)
    clause_no = str(hit.clause_no or "").strip()
    if clause_no:
        aliases.append(clause_no)
        if clause_no.startswith("第"):
            aliases.append(clause_no.replace("条", "").strip())
        else:
            aliases.append(f"第{clause_no}条")
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def allowed_numbers_for(
    route: SectionRoute, hits: list[clause_store.ClauseRef] | tuple[clause_store.ClauseRef, ...]
) -> ScopedAllowedNumbers:
    """允许集合按 ``clause_uid`` 分组：每组 = 该条款摘录数值 ∪ 项目事实值。

    不跨条款合并 —— 否则「A 写 5m、B 写 8m」时把引用 A 的句子改成 8m 会漏拦。
    项目事实值额外单列一组，供未引用任何条款的句子使用。
    """
    clause_items = [
        (str(hit.clause_uid or ""), _clause_label(hit), _clause_aliases(hit), str(hit.excerpt or ""))
        for hit in hits or ()
    ]
    return build_scoped_allowed_numbers(clause_items, list(route.fact_values.values()))


def verify_standard_text(text: str, allowed: AllowedNumbers | ScopedAllowedNumbers) -> NumericLockResult:
    """规范驱动段落的数值锁校验（不改写正文，改写由 numeric_lock.enforce_text 完成）。"""
    return check_text(text, allowed)


def _cites_fact(text: str, fact_values: dict[str, str]) -> bool:
    """正文是否引用了任一项目事实的具体数据（数值优先，否则取前 6 字比对）。"""
    text_tokens = extract_numbers(text or "")
    for value in fact_values.values():
        value = str(value or "")
        value_tokens = extract_numbers(value)
        if value_tokens:
            allowed = AllowedNumbers(tuple(value_tokens))
            if any(allowed.contains(token) for token in text_tokens):
                return True
        elif value.strip() and value.strip()[:6] in (text or ""):
            return True
    return False


def verify_experience_text(
    text: str,
    route: SectionRoute,
    blacklist: tuple[str, ...] = DEFAULT_VAGUE_PHRASES,
) -> list[dict]:
    """经验驱动段落的两条硬约束 + 事实缺失可见性，返回缺陷清单。"""
    defects: list[dict] = []
    if not route.fact_values:
        defects.append({"code": "no_fact_cited", "detail": "fact_values 为空，无可引用的项目事实"})
    elif not _cites_fact(text, route.fact_values):
        defects.append({"code": "no_fact_cited", "detail": "正文未引用任何项目事实值"})

    for phrase in blacklist:
        phrase = str(phrase or "")
        if phrase and phrase in (text or ""):
            defects.append({"code": "vague_phrase", "detail": phrase})

    if route.fact_status in ("missing", "partial") and MISSING_NOTICE_MARKER not in (text or ""):
        defects.append(
            {
                "code": "missing_fact_unmarked",
                "detail": "事实缺失未显式标注：" + "、".join(route.missing_deps or ()),
            }
        )
    return defects


def blacklist_from_settings() -> tuple[str, ...]:
    """从配置读取黑名单（逗号 / 分号 / 顿号分隔）；空值回退默认清单。"""
    raw = str(getattr(settings, "hybrid_vague_blacklist", "") or "")
    items = tuple(item.strip() for item in re.split(r"[,，;；、]", raw) if item.strip())
    return items or DEFAULT_VAGUE_PHRASES
