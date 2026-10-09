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
# ④ 半角方括号中文占位（F3）：[就近综合医院] / [邻近专科医院/门诊部]
#    —— AI 缺具体名称时的又一种写法。要求括号内至少含 1 个中文字符且以中文
#    开头，避免误报英文引用、公式下标与纯符号；长度 ≤30，容忍内部 / 、空格。
RE_BRACKET = re.compile(r"\[(?=[^\]]*[\u4e00-\u9fff])[\u4e00-\u9fff][^\]\n]{0,29}?\]")

# 上下文片段：匹配点前后各取的字符数（供清单里预览，辅助人工定位）
_SNIPPET_RADIUS = 18
# 清单默认逐条上限（防御极端长文撑爆响应体；聚合维度不设限）
DEFAULT_OCCURRENCE_CAP = 200

# 占位符种类 → 人类可读说明（前端 Tooltip / 文案用）
KIND_LABELS = {
    "formatted": "规范字段占位【待补充：字段名】",
    "bare": "裸占位标记（缺少字段名，无法定位到具体参数）",
    "fuzzy": "模糊占位符 ××/xx（提示词禁止写法，应改为【待补充：字段名】）",
    "bracket": "方括号中文占位 [中文]（缺具体名称，应落实为真实信息或改规范占位）",
}


def _make_snippet(content: str, start: int, end: int) -> str:
    """截取匹配点前后的上下文片段（跨行空白折叠为单空格，便于单行展示）。"""
    lo = max(0, start - _SNIPPET_RADIUS)
    hi = min(len(content), end + _SNIPPET_RADIUS)
    raw = content[lo:hi]
    return re.sub(r"\s+", " ", raw).strip()


def scan_occurrences(content: str, include_bracket: bool | None = None) -> list[dict]:
    """扫描单章正文，返回逐条占位符出现记录（纯函数，非法输入返回空表）。

    每条记录：{"kind": formatted|bare|fuzzy|bracket, "field": 字段名或空串,
    "snippet": 上下文片段}

    ``include_bracket``：是否扫描半角方括号中文占位（F3）。None 时读取配置
    ``placeholder_scan_bracket``（默认 True）；显式传 False 可回退旧口径。
    """
    if not isinstance(content, str) or not content:
        return []
    if include_bracket is None:
        try:
            from app.config import settings
            include_bracket = bool(settings.placeholder_scan_bracket)
        except Exception:  # 配置不可用时按开启处理（补齐漏检，不静默放过）
            include_bracket = True
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
    if include_bracket:
        for m in RE_BRACKET.finditer(content):
            inner = m.group(0)[1:-1].strip()
            hits.append({"kind": "bracket", "field": inner,
                         "snippet": _make_snippet(content, m.start(), m.end())})
    return hits


def build_placeholder_report(sections: list[dict],
                             occurrence_cap: int = DEFAULT_OCCURRENCE_CAP,
                             include_bracket: bool | None = None) -> dict:
    """把多章扫描结果聚合成《待补充清单》（纯函数）。

    sections 每项至少含 id / title（可选 content / sort_order）。
    返回结构：
      total / formatted_total / bare_total / fuzzy_total / bracket_total /
      field_count / section_count
      by_field:    [{field, count, section_ids, section_titles}]（按出现次数降序）
      by_section:  [{section_id, title, count, fields}]（按次数降序）
      occurrences: [{section_id, section_title, kind, field, snippet}]（截断至 cap）
      truncated:   bool（逐条记录是否被 cap 截断）

    ``include_bracket``：是否统计半角方括号中文占位（F3），None 时跟随配置。
    """
    report: dict = {
        "total": 0, "formatted_total": 0, "bare_total": 0, "fuzzy_total": 0,
        "bracket_total": 0,
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
        hits = scan_occurrences(sec.get("content"), include_bracket)
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
            elif kind == "fuzzy":
                report["fuzzy_total"] += 1
            elif kind == "bracket":
                report["bracket_total"] += 1
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


async def _resolve_project_id_for_scope(db, scheme_id: str) -> str:
    """反查方案所属 project_id（供事实作用域查询用）；失败返回空串。

    ✅ 2026-10-06：直接复用 ``facts_extractor.resolve_scheme_project_id``
    （本模块不再自带一份反查）。该函数已带 R13 判空与分级日志
    （未传 scheme_id → DEBUG；查不到 / 异常 → WARNING）。
    """
    try:
        from app.services.facts_extractor import resolve_scheme_project_id
        return await resolve_scheme_project_id(db, scheme_id)
    except Exception as e:  # pragma: no cover - 降级为仅方案级
        logger.warning("反查方案所属项目失败（全局事实仅按方案级读取）: %s", e)
        return ""


async def build_rerun_plan(scheme_id: str, db) -> dict:
    """重跑计划（DB 封装）：清单扫描 + 可注入语料读取 + 纯函数判定。

    可注入语料与生成侧同口径：
    - 全局事实：复用 ``facts_extractor.build_injectable_facts_query()``（唯一出口），
      **同时**约束门控（四条件 fail-closed：has_conflict=0 AND is_resolved=1
      AND is_simulated=0 AND is_stale=0）与作用域（方案级 + 项目共享级）——
      与 _render_facts_text / _load_facts_rows / export 附录逐字同源
      （被过滤的事实不参与生成，也不算"已补齐"）；
    - 解析提取：``bid_analysis_items.status='success'`` 的成果全文。
    查询失败降级为"无语料"（所有字段不可补齐，只给清单不给重跑建议），
    绝不阻断调用方。

    ✅ BUG 修复（2026-09-30 · 口径分叉 → 假"可重跑"）：旧实现硬编码
    ``is_resolved=1 AND has_conflict=0``，漏掉 is_simulated=0 / is_stale=0，
    而本函数 docstring 却宣称"与 _render_facts_text 的过滤口径一致"——
    **声明与实现不符**。后果：一条 is_stale=1（来源资料已变化，
    _mark_project_facts_stale 批量置位且**不清 is_resolved**，故
    resolved=1 且 stale=1 是可达状态）或 is_simulated=1（AI 编造值）的事实在
    生成侧被正确排除、在本处却被算作"可注入语料" → 字段标 fillable=true →
    章节标 rerunnable → 前端提示"重跑本节即可消除占位"，而用户重跑后占位
    **原样还在**（语料里本就没有它）。方向恰好是"让用户白跑一遍"。
    与 §4.11.2「JSON 失败哨兵被当成已完成 → 三重假绿」同源。
    修法：不再本地硬编码，改为调用唯一出口，从结构上消除分叉可能。
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
    # ✅ 2026-10-06 作用域对齐：本查询此前只按 `scheme_id=?` 取事实，
    # **不含项目共享级**（global_facts.scheme_id 为空、由 project_id 归属的那批）。
    # 其余 6 个消费方（正文注入 sse_handlers / 目录生成 / 导出附录 export /
    # 预检 compliance / 覆盖度台账 input_coverage / 跨模块桥接）都经
    # ``build_injectable_facts_query`` 取「方案级 + 项目共享级」。
    # 口径分叉的实际后果：项目共享事实里若含占位标记，本报告**看不到** →
    # 「占位符可被重跑清除」的结论偏乐观，章节被标 rerunnable 但重跑也不消除。
    # 现改为复用同一出口（含门控 + 作用域），不再本地拼 WHERE。
    try:
        from app.services.facts_extractor import FACTS_GT_COLUMN, build_injectable_facts_query
        # ⚠️ columns **必须**含 FACTS_GT_COLUMN（带 AS gt）：该出口的 SQL 末尾
        #   固定 ``ORDER BY gt, title``，缺别名会报 "no such column: gt"，
        #   而异常被下面的 except 吞掉 → 语料整体为空 → 所有字段判不可补齐
        #   （「假不可重跑」，与本模块要修的「假可重跑」同族，只是方向相反）。
        sql, params = build_injectable_facts_query(
            scheme_id, await _resolve_project_id_for_scope(db, scheme_id),
            f"{FACTS_GT_COLUMN}, group_title, title, content")
    except Exception as e:  # pragma: no cover - 降级为方案级（fail-soft）
        logger.warning("构造全局事实查询失败（降级为仅方案级）: %s", e, exc_info=True)
        try:
            from app.services.facts_extractor import get_facts_inject_where
            inject_where = get_facts_inject_where()
        except Exception:  # pragma: no cover - 兜底仍 fail-closed，绝不 fail-open
            logger.warning("取全局事实注入门控失败（按保守口径过滤该源）")
            inject_where = (
                "has_conflict=0 AND is_resolved=1 AND is_simulated=0 AND is_stale=0")
        sql = ("SELECT group_title, title, content FROM global_facts"
               f" WHERE scheme_id=? AND {inject_where}")
        params = (scheme_id,)
    try:
        cur = await db.execute(sql, params)
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
