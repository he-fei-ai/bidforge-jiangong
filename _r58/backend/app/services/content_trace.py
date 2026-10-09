"""正文生成可追溯性内部标记（2026-10-01）。

定位
----
模糊生成是「合理概括 / 归纳 / 泛化」，对读者不可见；但交付前复核必须能回答：
「这一章里哪些内容是拿全局事实精准写的、哪些是模糊生成的、为什么模糊、
对应哪个数据来源」。

三条硬约束（产品要求）：

1. **只落库、不落正文**：标记写入 ``sections.last_generation_report`` 的
   ``trace`` 键，**绝不插入 ``sections.content``** —— 导出 DOCX 读取的是
   ``content``，因此成稿里不会出现任何模糊生成标记（由护栏测试断言）。
2. **生成后确定性派生**：零 AI、零成本、可复算。标记由
   ``content_standard.standard_report`` 在锁外一次性产出，
   与校验器**共享同一份正文扫描结果**（不重复正则扫描，避免 O(n²)）。
3. **只读消费**：审核与预检模块只读（``GET /report``、``/report-summary``、
   ``/export/check``），不改写正文、不阻断生成、不参与导出渲染。

标记条目（``items`` 元素）字段：

    id              稳定序号（tr-0001 …，按正文出现顺序）
    section_id      章节 ID（落库时由调用方补，纯函数默认空串）
    section_title   章节标题
    position        人类可读位置，形如 "L12 P3"（行号 / 段落号）
    char_start      起始字符偏移（相对**扫描文本**，非原文）
    char_end        结束字符偏移
    excerpt         上下文片段（人工定位主键，跨行空白已折叠）
    generation_type precise | fuzzy | placeholder_residue
    category        数值/名称/时间/数量/承诺/技术参数/材料规格/工序流程 类别（无则空串）
    data_source     数据来源表述（精准=具体事实标题；模糊=规则表口径）
    fuzzy_reason    模糊原因（精准内容为空串）
    severity        info | warn | error（复核优先级，不影响生成）

坐标口径说明：``char_start`` / ``char_end`` 是 ``strip_code_blocks`` 剔除围栏
代码块后的**扫描文本**坐标，与 ``content`` 原文坐标不一致（图表代码块内的
内容不参与事实校验与标记）。因此人工定位以 ``excerpt`` + ``position`` 为准。
"""
from __future__ import annotations

import re

from app.services.content_fuzzy import FUZZY_CATEGORIES, FUZZY_UNDETECTABLE

#: 内部标记条目上限（防御极端长章撑爆 last_generation_report 字段）
TRACE_MAX_ITEMS = 200

#: 生成类型值域
GENERATION_PRECISE = "precise"
GENERATION_FUZZY = "fuzzy"
GENERATION_PLACEHOLDER = "placeholder_residue"

#: 标记条目键集合（护栏用：防止字段漂移）
TRACE_ITEM_KEYS: tuple[str, ...] = (
    "id", "section_id", "section_title", "position",
    "char_start", "char_end", "excerpt",
    "generation_type", "category", "data_source", "fuzzy_reason", "severity",
)

#: 段落切分（Markdown 空行）
_PARA_SPLIT_RE = re.compile(r"\n[ \t]*\n")

_EXCERPT_RADIUS = 16


def locate_position(scanned: str, char_start: int) -> str:
    """把字符偏移换算成 "L{行} P{段}" 形式（纯函数，越界自动收敛）。"""
    if not isinstance(scanned, str):
        return "L0 P0"
    pos = min(max(int(char_start or 0), 0), len(scanned))
    prefix = scanned[:pos]
    line = prefix.count("\n") + 1
    para = len(_PARA_SPLIT_RE.findall(prefix)) + 1
    return f"L{line} P{para}"


def make_excerpt(scanned: str, char_start: int, char_end: int,
                 radius: int = _EXCERPT_RADIUS) -> str:
    """截取上下文片段（跨行空白折叠为单空格，便于单行展示）。"""
    if not isinstance(scanned, str):
        return ""
    lo = max(0, int(char_start or 0) - radius)
    hi = min(len(scanned), int(char_end or 0) + radius)
    frag = re.sub(r"\s+", " ", scanned[lo:hi]).strip()
    return f"…{frag}…" if frag else ""


def _excerpt_of(scanned: str, start: int, end: int) -> str:
    return make_excerpt(scanned, start, end)


def _new_item(index: int, *, section_id: str, section_title: str, scanned: str,
              char_start: int, char_end: int, generation_type: str,
              category: str = "", data_source: str = "",
              fuzzy_reason: str = "", severity: str = "info") -> dict:
    """构造一条标记（键集合严格等于 ``TRACE_ITEM_KEYS``）。"""
    return {
        "id": f"tr-{index:04d}",
        "section_id": section_id or "",
        "section_title": section_title or "",
        "position": locate_position(scanned, char_start),
        "char_start": int(char_start or 0),
        "char_end": int(char_end or 0),
        "excerpt": _excerpt_of(scanned, char_start, char_end),
        "generation_type": generation_type,
        "category": category or "",
        "data_source": data_source or "",
        "fuzzy_reason": fuzzy_reason or "",
        "severity": severity or "info",
    }



def build_trace(*, scanned: str, standard: str = "",
                section_id: str = "", section_title: str = "",
                fact_numbers: list | None = None, fact_models: list | None = None,
                body_numbers: list | None = None, body_models: list | None = None,
                fuzzy_expressions: list | None = None,
                placeholder_hits: list | None = None) -> dict:
    """把「本章正文 × 本章事实」派生成内部标记清单（纯函数、零 AI、零成本）。

    判定口径：
      1. 正文数值/型号 token **命中事实** → ``precise``，data_source 指向具体事实标题；
      2. 正文数值/型号 token **未命中任何事实** → ``fuzzy`` + ``severity=warn``
         （这是「编造关键数据」风险的唯一定位线索，必须有记录可查）；
      3. 正文中的模糊表述片段（概数 / 区间 / 相对时间 / 岗位称谓 / 技术原则）
         → ``fuzzy``，category / data_source / fuzzy_reason 取自
         ``content_fuzzy.FUZZY_CATEGORIES`` 同一张表；
      4. 残留占位标记 → ``placeholder_residue`` + ``severity=error``。

    重叠收敛：落在模糊表述区间内的数值 token 不再单独成条（「约 13m」算一条，
    不重复算「13m 无事实支撑」），避免同一处内容刷两条标记。

    任何异常都返回降级形态（available=False），绝不抛出影响正文生成。
    """
    items: list[dict] = []
    try:
        text = scanned or ""
        fact_numbers = fact_numbers or []
        fact_models = fact_models or []
        body_numbers = body_numbers or []
        body_models = body_models or []
        fuzzy_expressions = fuzzy_expressions or []
        placeholder_hits = placeholder_hits or []

        # ---- 事实侧索引：(值, 单位) / 型号 → 事实标题集合 ----
        backed_num: dict[tuple, list[str]] = {}
        backed_model: dict[str, list[str]] = {}
        for fn in fact_numbers:
            key = (float(fn.get("value") or 0), str(fn.get("unit") or ""))
            t = str(fn.get("title") or "")
            backed_num.setdefault(key, [])
            if t and t not in backed_num[key]:
                backed_num[key].append(t)
        for fm in fact_models:
            tok = str(fm.get("token") or "")
            if not tok:
                continue
            title = str(fm.get("title") or "")
            backed_model.setdefault(tok, [])
            if title and title not in backed_model[tok]:
                backed_model[tok].append(title)

        # ---- 模糊表述区间（用于重叠收敛）----
        fuzzy_spans = [(int(f.get("char_start") or 0), int(f.get("char_end") or 0))
                       for f in fuzzy_expressions]

        def _inside_fuzzy(start: int, end: int) -> bool:
            return any(start < fe and end > fs for fs, fe in fuzzy_spans)

        def _add(gtype: str, start: int, end: int, category: str = "",
                 data_source: str = "", fuzzy_reason: str = "",
                 severity: str = "info") -> None:
            items.append(_new_item(
                len(items) + 1, section_id=section_id, section_title=section_title,
                scanned=text, char_start=start, char_end=end,
                generation_type=gtype, category=category,
                data_source=data_source, fuzzy_reason=fuzzy_reason,
                severity=severity))

        # ---- 模糊表述片段 ----
        for fx in fuzzy_expressions:
            cat = str(fx.get("category") or "")
            cat_info = FUZZY_CATEGORIES.get(cat, {})
            _add(GENERATION_FUZZY, int(fx.get("char_start") or 0),
                 int(fx.get("char_end") or 0), category=cat,
                 data_source=cat_info.get("data_source", ""),
                 fuzzy_reason=cat_info.get("reason", ""), severity="info")

        # ---- 正文数值 ----
        for bn in body_numbers:
            span = bn.get("span") or (0, 0)
            if _inside_fuzzy(span[0], span[1]):
                continue  # 已作为模糊表述整体记录
            key = (float(bn.get("value") or 0), str(bn.get("unit") or ""))
            titles = backed_num.get(key)
            if titles:
                _add(GENERATION_PRECISE, span[0], span[1], category="number",
                     data_source="全局事实：" + " / ".join(titles[:2]), severity="info")
            else:
                info = FUZZY_CATEGORIES["number"]
                _add(GENERATION_FUZZY, span[0], span[1], category="number",
                     data_source=info["data_source"],
                     fuzzy_reason="事实未给出该数值（正文出现具体值，需人工复核是否编造）",
                     severity="warn")
        # ---- 正文型号 ----
        for bm in body_models:
            span = bm.get("span") or (0, 0)
            if _inside_fuzzy(span[0], span[1]):
                continue
            tok = str(bm.get("token") or "")
            titles = backed_model.get(tok)
            if titles:
                _add(GENERATION_PRECISE, span[0], span[1], category="name",
                     data_source="全局事实：" + " / ".join(titles[:2]), severity="info")
            else:
                info = FUZZY_CATEGORIES["name"]
                _add(GENERATION_FUZZY, span[0], span[1], category="name",
                     data_source=info["data_source"],
                     fuzzy_reason="事实未给出该型号（正文出现具体型号，需人工复核是否编造）",
                     severity="warn")

        # ---- 残留占位标记 ----
        for ph in placeholder_hits:
            _add(GENERATION_PLACEHOLDER, int(ph.get("char_start") or 0),
                 int(ph.get("char_end") or 0),
                 fuzzy_reason="正文残留占位标记（应为完整正文，不留待补充标记）",
                 severity="error")

        # 按正文顺序稳定排序并重编 id
        items.sort(key=lambda d: (d["char_start"], d["char_end"]))
        for i, it in enumerate(items, start=1):
            it["id"] = f"tr-{i:04d}"

        truncated = len(items) > TRACE_MAX_ITEMS
        if truncated:
            items = items[:TRACE_MAX_ITEMS]
        return {
            "available": True,
            "degraded": False,
            "standard": str(standard or ""),
            "section_id": section_id or "",
            "section_title": section_title or "",
            "items": items,
            "summary": trace_summary(items, truncated=truncated),
        }
    except Exception:
        return {
            "available": False, "degraded": True, "standard": str(standard or ""),
            "section_id": section_id or "", "section_title": section_title or "",
            "items": [], "summary": empty_summary(),
        }



def empty_summary() -> dict:
    """空标记集的汇总形态（降级 / 无内容时返回）。"""
    return {
        "total": 0, "precise_count": 0, "fuzzy_count": 0, "warn_count": 0,
        "error_count": 0, "placeholder_residue_count": 0,
        "by_category": {}, "by_reason": {}, "truncated": False,
    }


def trace_summary(items: list | None, *, truncated: bool = False) -> dict:
    """汇总标记清单（纯函数，供报告与端点聚合复用）。"""
    items = items or []
    by_cat: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for it in items:
        gt = str(it.get("generation_type") or "")
        cat = str(it.get("category") or "")
        if gt == GENERATION_FUZZY and cat:
            by_cat[cat] = by_cat.get(cat, 0) + 1
        reason = str(it.get("fuzzy_reason") or "")
        if reason and gt == GENERATION_FUZZY:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "total": len(items),
        "precise_count": sum(1 for it in items
                             if it.get("generation_type") == GENERATION_PRECISE),
        "fuzzy_count": sum(1 for it in items
                           if it.get("generation_type") == GENERATION_FUZZY),
        "warn_count": sum(1 for it in items if it.get("severity") == "warn"),
        "error_count": sum(1 for it in items if it.get("severity") == "error"),
        "placeholder_residue_count": sum(1 for it in items
                                         if it.get("generation_type") == GENERATION_PLACEHOLDER),
        "by_category": dict(sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_reason": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        "truncated": bool(truncated),
    }


#: 八类中无法确定性判定的类别（供报告 / 文档说明，不产生误报）
UNDETECTABLE_CATEGORIES: tuple[str, ...] = FUZZY_UNDETECTABLE


def collect_scheme_trace(sections: list[dict], item_cap: int = 300) -> dict:
    """把方案内各章的报告 JSON 汇总成「模糊生成清单」（审核 / 预检只读消费）。

    ``sections`` 每项需含 ``id`` / ``title`` / ``last_generation_report``
    （后者为 JSON 文本；解析失败或无 trace 的章节不计入 ``section_count``，
    全部非法输入计入 ``skipped``）。
    """
    out_items: list[dict] = []
    section_rows: list[dict] = []
    skipped = 0
    for sec in sections or []:
        if not isinstance(sec, dict):
            skipped += 1
            continue
        raw = sec.get("last_generation_report") or ""
        report: dict = {}
        if isinstance(raw, dict):
            report = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                import json as _json
                loaded = _json.loads(raw)
                report = loaded if isinstance(loaded, dict) else {}
            except Exception:
                report = {}
        trace = report.get("trace") or {}
        items = trace.get("items") if isinstance(trace, dict) else None
        if not items:
            continue
        sid = str(sec.get("id") or "")
        cnt = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            it.setdefault("section_id", sid)
            it.setdefault("section_title", str(sec.get("title") or ""))
            out_items.append(it)
            cnt += 1
        section_rows.append({"section_id": sid, "title": str(sec.get("title") or ""),
                             "total": cnt})
    truncated = len(out_items) > item_cap
    if truncated:
        out_items = out_items[:item_cap]
    return {**trace_summary(out_items, truncated=truncated),
            "section_count": len(section_rows), "skipped": skipped,
            "sections": section_rows}
