"""《待补充清单》逐条扫描与聚合（2026-09-24，治 F 层：人工补录兜底）

背景：正文生成按「数据真实性红线」把无法获取的关键数据统一写作
``【待补充：参数名】``（见 services/ai/prompts/content.py）。导出侧
``routers/export.py::audit_content`` 只产出**聚合计数**（placeholder warn N 处），
无法回答用户的三个高频问题：
  1. 到底缺了哪些字段？（按字段名聚合）
  2. 都出现在哪些章节？（按章节聚合，支持前端一键跳转）
  3. 有没有不规范的模糊占位（××/xx）需要回改？（规范度检查）

本模块提供：
1. ``scan_occurrences``：单章正文的逐条扫描（纯函数）；
2. ``build_placeholder_report``：多章聚合 → 《待补充清单》（纯函数）；
3. ``build_report_for_scheme``：从库读取章节并产出清单（DB 封装）。

设计约束：
- **只扫描、不篡改**：与 audit_content 的「只体检不改正文」原则一致，
  清单仅供人工补录定位，绝不自动改写正文；
- 纯函数与 DB 读取分离，非法输入一律降级为空报告，绝不抛异常打断调用方；
- 占位符口径与 export.py::_AUDIT_RULES 的 placeholder 规则同源（三类：
  规范字段占位 / 裸占位标记 / 模糊占位），但正则在此独立定义 —— services 层
  不得反向 import routers 层（避免循环依赖）。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("placeholder_inventory")

# ---- 三类占位符（口径与 export.py::_AUDIT_RULES placeholder 规则对齐） ----
# ① 规范占位：【待补充：字段名】（半角/全角冒号均可，字段名 ≤60 字，容忍两端空白）
RE_FORMATTED = re.compile(r"【待补充\s*[:：]\s*([^】]{1,60}?)\s*】")
# ② 裸占位标记：【待补充】/【待填写】（facts_extractor placeholder 模式产出【待填写】）
RE_BARE = re.compile(r"【(?:待补充|待填写)】")
# ③ 模糊占位：×× / xx / XX（提示词明令禁止的写法，必须回改为规范格式）
RE_FUZZY = re.compile(r"××+|(?<![A-Za-z0-9])[xX]{2}(?![A-Za-z0-9])")

# 上下文片段：匹配点前后各取的字符数（供清单里预览，辅助人工定位）
_SNIPPET_RADIUS = 18
# 清单默认逐条上限（防御极端长文撑爆响应体；聚合维度不设限）
DEFAULT_OCCURRENCE_CAP = 200

# 占位符种类 → 人类可读说明（前端 Tooltip / 文案用）
KIND_LABELS = {
    "formatted": "规范字段占位【待补充：字段名】",
    "bare": "裸占位标记（缺少字段名，无法定位到具体参数）",
    "fuzzy": "模糊占位符 ××/xx（提示词禁止写法，应改为【待补充：字段名】）",
}


def _make_snippet(content: str, start: int, end: int) -> str:
    """截取匹配点前后的上下文片段（跨行空白折叠为单空格，便于单行展示）。"""
    lo = max(0, start - _SNIPPET_RADIUS)
    hi = min(len(content), end + _SNIPPET_RADIUS)
    raw = content[lo:hi]
    return re.sub(r"\s+", " ", raw).strip()


def scan_occurrences(content: str) -> list[dict]:
    """扫描单章正文，返回逐条占位符出现记录（纯函数，非法输入返回空表）。

    每条记录：{"kind": formatted|bare|fuzzy, "field": 字段名或空串,
    "snippet": 上下文片段}
    """
    if not isinstance(content, str) or not content:
        return []
    hits: list[dict] = []
    for m in RE_FORMATTED.finditer(content):
        hits.append({
            "kind": "formatted",
            "field": (m.group(1) or "").strip(),
            "snippet": _make_snippet(content, m.start(), m.end()),
        })
    for m in RE_BARE.finditer(content):
        hits.append({"kind": "bare", "field": "",
                     "snippet": _make_snippet(content, m.start(), m.end())})
    for m in RE_FUZZY.finditer(content):
        hits.append({"kind": "fuzzy", "field": "",
                     "snippet": _make_snippet(content, m.start(), m.end())})
    return hits


def build_placeholder_report(sections: list[dict],
                             occurrence_cap: int = DEFAULT_OCCURRENCE_CAP) -> dict:
    """把多章扫描结果聚合成《待补充清单》（纯函数）。

    sections 每项至少含 id / title（可选 content / sort_order）。
    返回结构：
      total / formatted_total / bare_total / fuzzy_total / field_count / section_count
      by_field:    [{field, count, section_ids, section_titles}]（按出现次数降序）
      by_section:  [{section_id, title, count, fields}]（按出现次数降序）
      occurrences: [{section_id, section_title, kind, field, snippet}]（截断至 cap）
      truncated:   bool（逐条记录是否被 cap 截断）
    """
    report: dict = {
        "total": 0, "formatted_total": 0, "bare_total": 0, "fuzzy_total": 0,
        "field_count": 0, "section_count": 0,
        "by_field": [], "by_section": [], "occurrences": [],
        "truncated": False,
    }
    if not isinstance(sections, list):
        return report

    # ---- 逐章扫描（聚合用 dict，输出前统一排序） ----
    field_stat: dict[str, dict] = {}     # 字段名 → {count, section_ids, section_titles}
    section_stat: list[dict] = []        # 章节聚合（保持扫描顺序，排序后输出）
    occurrences: list[dict] = []
    cap = max(int(occurrence_cap), 0)
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        sid = str(sec.get("id") or "")
        title = str(sec.get("title") or "")
        hits = scan_occurrences(sec.get("content"))
        if not hits:
            continue
        sec_fields: list[str] = []
        seen_fields: set[str] = set()
        for h in hits:
            kind = h.get("kind") or ""
            report["total"] += 1
            if kind == "formatted":
                report["formatted_total"] += 1
            elif kind == "bare":
                report["bare_total"] += 1
            else:
                report["fuzzy_total"] += 1
            rec = {"section_id": sid, "section_title": title,
                   "kind": kind, "field": h.get("field") or "",
                   "snippet": h.get("snippet") or ""}
            if len(occurrences) < cap:
                occurrences.append(rec)
            elif len(occurrences) == cap:
                report["truncated"] = True
            # 规范占位才进字段聚合（裸/模糊占位没有字段名可聚合）
            if kind == "formatted" and rec["field"]:
                st = field_stat.setdefault(rec["field"], {
                    "count": 0, "section_ids": [], "section_titles": []})
                st["count"] += 1
                if sid not in st["section_ids"]:
                    st["section_ids"].append(sid)
                    st["section_titles"].append(title)
                if rec["field"] not in seen_fields:
                    seen_fields.add(rec["field"])
                    sec_fields.append(rec["field"])
        section_stat.append({
            "section_id": sid, "title": title,
            "count": len(hits), "fields": sec_fields})

    report["occurrences"] = occurrences
    report["field_count"] = len(field_stat)
    report["section_count"] = len(section_stat)
    # 按出现次数降序、次键按名称升序（确定性输出，便于快照对比）
    report["by_field"] = sorted(
        ({"field": k, **v} for k, v in field_stat.items()),
        key=lambda e: (-e["count"], e["field"]))
    report["by_section"] = sorted(
        section_stat, key=lambda e: (-e["count"], e["title"]))
    return report


async def build_report_for_scheme(scheme_id: str, db,
                                  occurrence_cap: int = DEFAULT_OCCURRENCE_CAP) -> dict:
    """从库读取方案全部章节并产出《待补充清单》（供 REST 端点调用）。

    列清单与 export.py::collect_export_issues 同源（含 sort_order / content），
    确保两处看到的是同一批章节；查询失败降级为空报告（不阻断调用方）。
    """
    try:
        cur = await db.execute(
            "SELECT id, parent_id, level, sort_order, title, status, "
            "word_count, content FROM sections"
            " WHERE scheme_id=? ORDER BY sort_order, level, id", (scheme_id,))
        sections = [dict(r) for r in (await cur.fetchall() or [])]
        return build_placeholder_report(sections, occurrence_cap=occurrence_cap)
    except Exception as e:  # 审计类旁路：绝不阻断主流程
        logger.warning("《待补充清单》扫描失败（降级为空报告）: %s", e)
        return build_placeholder_report([], occurrence_cap=occurrence_cap)


# ---------------------------------------------------------------------------
# ✅ 重跑计划（2026-09-24，治 F 层第 3 条：补录后只重跑受影响章节）
# ---------------------------------------------------------------------------
def _norm_field(text: str) -> str:
    """字段名归一：去空白 + 小写（全半角一致性由调用方文本保证，宁漏报不误报）。"""
    return re.sub(r"\s+", "", str(text or "")).lower()


def build_rerun_plan_from_report(report: dict, sections: list[dict],
                                 injectable_corpus: list[str]) -> dict:
    """由《待补充清单》+ 可注入语料 → 重跑计划（纯函数）。

    判定口径（宁漏报不误报）：
    - 字段「可补齐」（fillable）：归一后的字段名**子串命中**任一可注入语料
      （全局事实的 分组标题/标题/内容 或 解析提取 success 成果全文）——
      命中即说明补录后的数据源里已有该参数，重跑对应章节即可消除占位；
    - 章节「可重跑」（rerunnable）：必须是**叶子章节**（select_target_leaves
      对父节点只取后代叶子，父节点自身正文不会被重写），且其规范占位中
      **至少一个字段可补齐**。裸标记/模糊占位没有字段名，不参与判定
      （需人工回改正文）。

    injectable_corpus: 已拼接好的可注入文本块列表（DB 读取在调用方完成，
    保持本函数纯函数属性便于单测）。
    """
    plan: dict = {
        "fields": [], "sections": [],
        "rerunnable_sections": [], "rerunnable_count": 0,
        "fillable_field_count": 0, "total_field_count": 0,
    }
    if not isinstance(report, dict) or not isinstance(sections, list):
        return plan
    corpus_norm = [_norm_field(b) for b in (injectable_corpus or []) if b]

    # ---- 字段级可补性 ----
    field_fill: dict[str, bool] = {}
    for e in report.get("by_field") or []:
        field = str(e.get("field") or "")
        if not field:
            continue
        nf = _norm_field(field)
        fillable = any(nf and nf in blob for blob in corpus_norm)
        field_fill[field] = fillable
        plan["fields"].append({"field": field, "fillable": fillable})
    plan["total_field_count"] = len(field_fill)
    plan["fillable_field_count"] = sum(1 for v in field_fill.values() if v)

    # ---- 叶子判定（与 content_utils.select_target_leaves 的父 → 子映射同构）----
    children_map: dict[str, list[str]] = {}
    for s in sections:
        children_map.setdefault(str(s.get("parent_id") or ""), []).append(
            str(s.get("id") or ""))

    for e in report.get("by_section") or []:
        sid = str(e.get("section_id") or "")
        if not sid:
            continue
        is_leaf = sid not in children_map
        fields_detail = [{"field": f, "fillable": field_fill.get(f, False)}
                         for f in e.get("fields") or []]
        fill_count = sum(1 for d in fields_detail if d["fillable"])
        rerunnable = bool(is_leaf and e.get("count") and fields_detail
                          and fill_count > 0)
        plan["sections"].append({
            "section_id": sid, "title": e.get("title") or "",
            "count": e.get("count") or 0, "is_leaf": is_leaf,
            "fields": fields_detail, "fill_count": fill_count,
            "rerunnable": rerunnable,
        })
        if rerunnable:
            plan["rerunnable_sections"].append(sid)
    plan["rerunnable_count"] = len(plan["rerunnable_sections"])
    return plan


async def build_rerun_plan(scheme_id: str, db) -> dict:
    """重跑计划（DB 封装）：清单扫描 + 可注入语料读取 + 纯函数判定。

    可注入语料与生成侧同口径：
    - 全局事实：``is_resolved=1 AND has_conflict=0``（与 _render_facts_text
      的过滤口径一致——被过滤的事实不参与生成，也不算"已补齐"）；
    - 解析提取：``bid_analysis_items.status='success'`` 的成果全文。
    查询失败降级为"无语料"（所有字段不可补齐，只给清单不给重跑建议），
    绝不阻断调用方。
    """
    try:
        cur = await db.execute(
            "SELECT id, parent_id, level, sort_order, title, status, "
            "word_count, content FROM sections"
            " WHERE scheme_id=? ORDER BY sort_order, level, id", (scheme_id,))
        sections = [dict(r) for r in (await cur.fetchall() or [])]
    except Exception as e:
        logger.warning("重跑计划读取章节失败（降级为空计划）: %s", e)
        return build_rerun_plan_from_report({}, [], [])

    report = build_placeholder_report(sections)
    corpus: list[str] = []
    try:
        cur = await db.execute(
            "SELECT group_title, title, content FROM global_facts"
            " WHERE scheme_id=? AND is_resolved=1 AND has_conflict=0",
            (scheme_id,))
        for r in (await cur.fetchall() or []):
            corpus.append(" ".join(str(r[k] or "") for k in
                                   ("group_title", "title", "content")))
    except Exception as e:
        logger.warning("重跑计划读取全局事实失败（该源降级跳过）: %s", e)
    try:
        cur = await db.execute(
            "SELECT content FROM bid_analysis_items"
            " WHERE project_id=(SELECT project_id FROM schemes WHERE id=?)"
            " AND status='success'", (scheme_id,))
        for r in (await cur.fetchall() or []):
            corpus.append(str(r["content"] or ""))
    except Exception as e:
        logger.warning("重跑计划读取解析提取成果失败（该源降级跳过）: %s", e)

    return build_rerun_plan_from_report(report, sections, corpus)
