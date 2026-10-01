"""输入依据字段清单与调用对照表（差集分析）

背景（2026-09-23 四项依据改造）：目录/正文生成要求「完整调用解析提取结果与
全局事实，不丢失、不截断、不错误映射」。但链路里数据源多（bid_analysis_items
18 项、global_facts 按组分片、方案名称施工内容、编制要求），注入点各有预算
截断（project_brief/facts 的字符预算、事实过滤口径 has_conflict/is_resolved），
一旦某项成果没进提示词，此前只能靠事后肉眼比对正文才能发现。

本模块提供两件事：
1. ``build_inventory``：盘点三类数据源的「字段清单」——每项标注可用性状态
   （available / 缺有效内容 / 待人工裁决被过滤 / 矛盾被过滤），让"未被调用"
   从静默丢失变成显式台账；
2. ``audit_prompt_coverage``：把字段清单与「实际装配出的提示词文本」做对照，
   产出 已调用 / 缺失 / 截断 三态对照表（差集分析），供生成链路打 WARNING
   日志与 REST 端点按需查询。

设计约束：
- 只审计、不阻断：任何异常降级为空报告，绝不影响生成主流程；
- 纯逻辑与 DB 读取分离（audit 是纯函数，便于单测）；
- 判定"已调用"的依据是各渲染器的稳定锚点（format_downstream_context 的
  "## {label}" 小节标题、_render_facts_text 的 "### {group_title}" 分组标题），
  不依赖正文语义匹配，宁漏报不误报。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("input_coverage")

# 数据来源枚举（差集报告按此分组）
SRC_PARSE = "解析提取"       # bid_analysis_items（结构化提取 18 项）
SRC_FACTS = "全局事实"       # global_facts（按 group_title 聚合）
SRC_SCOPE = "方案名称施工内容"  # scheme_scope 抽取结果
SRC_REQ = "编制要求"         # schemes.config_json.requirements
# ✅ 2026-09-26（目录生成三项依据收敛）：方案名称解析出的**新增**维度
# （施工工序 / 施工工艺 / 施工对象）。原 SRC_SCOPE 只登记「主要施工内容」
# 一项，导致「方案名称 → 工序/工艺/对象」的字段清单在差集报告里完全不可见
# （既看不到有没有解析出来，也看不到有没有进提示词）。
SRC_BASIS = "方案名称解析"   # scheme_basis：process_steps / techniques / objects
SRC_STANDARDS = "编制依据规范"  # standards_registry 匹配的规范文本


@dataclass
class FieldEntry:
    """一个可注入下游的输入字段（差集分析的原子单位）。"""
    source: str
    key: str            # 数据源内唯一键（item_id / 事实分组 / 施工内容片段）
    label: str          # 人类可读名（提示词中应出现的锚点文本）
    chars: int = 0      # 源内容字符量（0=无有效内容）
    # available | empty | not_success | filtered_unresolved | filtered_conflict
    #   | filtered_stale | filtered_simulated
    # ✅ 2026-09-30 新增 filtered_stale / filtered_simulated：此前来源过期(is_stale=1)
    #   与 AI 编造值(is_simulated=1)两类被过滤事实**在台账里完全不可见**
    #   （既不计 available 也不计任何 filtered_*），恰好违背本模块"让静默丢失
    #   变显式台账"的设计目的。新增为 additive，旧调用方按前缀/枚举白名单
    #   过滤时行为不变。
    status: str = "available"
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "source": self.source, "key": self.key, "label": self.label,
            "chars": self.chars, "status": self.status, "note": self.note,
        }


@dataclass
class InputInventory:
    """字段清单（一次生成的输入台账）。"""
    entries: list[FieldEntry] = field(default_factory=list)
    # ✅ 代码审查 m3（2026-09-23）：某数据源查询异常被降级跳过时，源名登记于此，
    #    使“盘点空清单”与“确实无待调字段”可区分，避免审计误报“全绿”。
    degraded: list[str] = field(default_factory=list)

    def available(self) -> list[FieldEntry]:
        return [e for e in self.entries if e.status == "available"]

    def by_source(self, source: str) -> list[FieldEntry]:
        return [e for e in self.entries if e.source == source]

    def as_dict(self) -> dict:
        return {
            "total": len(self.entries),
            "available": len(self.available()),
            "degraded": list(self.degraded),
            "entries": [e.as_dict() for e in self.entries],
        }


async def build_inventory(db, project_id: str, scheme_id: str,
                          scope_items: list[str] | None = None,
                          requirements_text: str = "",
                          basis=None, standards_text: str = "") -> InputInventory:
    """盘点三类数据源的字段清单（解析提取 / 全局事实 / 施工内容+编制要求）。

    全局事实按 group_title 聚合为条目（逐条事实可达数百，聚合后仍可定位
    「哪一组数据没进提示词」），并区分被过滤的事实（未裁决矛盾 / 未经确认），
    这正是「无法获取的数据应标注待补充而非编造」的台账依据。

    ✅ 2026-09-26（目录生成三项依据收敛）：新增两个**可选**参数 ``basis``
    （``services.scheme_basis.SchemeBasis``）与 ``standards_text``，把
    「方案名称解析出的工序/工艺/对象」与「匹配到的编制依据规范」也纳入
    字段清单 —— 否则这两项依据在差集报告里完全不可见。不传时行为与旧版
    逐字节一致（向后兼容）。
    """
    inv = InputInventory()

    # ---- 1. 解析提取（bid_analysis_items）----
    try:
        from app.services.bid_analysis_service import is_missing_result
        cur = await db.execute(
            "SELECT item_id, label, output_type, status, content "
            "FROM bid_analysis_items "
            "WHERE project_id=? ORDER BY sort_order, item_id", (project_id,))
        for r in (await cur.fetchall() or []):
            content = (r["content"] or "").strip()
            status = (r["status"] or "").strip()
            # ✅ 审计可信度修复（2026-09-23 代码审查 M2）：消费端（
            #    _build_structured_brief / format_downstream_context）只下发
            #    status='success' 的成果，而 running/error 行**保留上一轮 content**
            #    （见 bid_analysis 写入路径），若不按 status 过滤会被判 available
            #    却永不进提示词 → 每轮生成虚假「缺失」告警，淹没有效信号。
            if status != "success":
                inv.entries.append(FieldEntry(
                    source=SRC_PARSE, key=r["item_id"],
                    label=r["label"] or r["item_id"], chars=len(content),
                    status="not_success",
                    note=f"状态 {status or '未知'}，按口径不下发"))
                continue
            # output_type 默认与消费端对齐（"markdown"），避免空类型时口径分歧
            ok = bool(content) and not is_missing_result(
                content, r["output_type"] or "markdown")
            inv.entries.append(FieldEntry(
                source=SRC_PARSE, key=r["item_id"], label=r["label"] or r["item_id"],
                chars=len(content),
                status="available" if ok else "empty",
                note="" if ok else "无有效提取内容（未跑/未提取到）"))
    except Exception as e:
        inv.degraded.append(SRC_PARSE)
        logger.warning("盘点解析提取字段清单失败（降级跳过该源）: %s", e)

    # ---- 2. 全局事实（按分组聚合，含被过滤事实的可见化）----
    # ✅ BUG 修复（2026-09-30 · 口径分叉 → 假"已调用"）：ok_cnt 旧口径只判
    #   has_conflict=0 AND is_resolved=1，漏 is_simulated=0 / is_stale=0。而
    #   生成侧 facts_extractor._FACTS_INJECT_WHERE 是四条件 fail-closed。
    #   后果与 placeholder_inventory 同源：is_stale=1（_mark_project_facts_stale
    #   批量置位且不清 is_resolved，故 resolved=1 且 stale=1 可达）或
    #   is_simulated=1（AI 编造值）的事实，生成侧**不注入**，此处却被计进
    #   ok_cnt → 差集报告标 available → audit_prompt_coverage 再按
    #   "### {分组标题}" 锚点判为**已调用**，而提示词里根本没有该事实。
    #   本模块的存在意义正是"把未被调用从静默丢失变成显式台账"，此处反而
    #   制造了静默丢失 + 假绿。
    #   另：旧实现完全没有 stale/simulated 的计数，被过滤的这两类事实
    #   **在台账里完全不可见**（既不计入 ok 也不计入任何 filtered_*）。
    #   修法：ok_cnt 改走唯一出口；并补两条 filtered_* 台账（additive，
    #   旧调用方读 available/not_injectable 的行为不变）。
    try:
        from app.services.facts_extractor import FACTS_GT_COLUMN, get_facts_inject_where
        # FACTS_GT_COLUMN 自带 `AS gt` 别名，不可重复拼接
        inject_where = get_facts_inject_where()
        # 门控是「A AND B AND C」形式，直接嵌入 CASE WHEN ... THEN 即可
        ok_case = inject_where
        cur = await db.execute(
            f"SELECT {FACTS_GT_COLUMN}, "
            f"SUM(CASE WHEN {ok_case} THEN 1 ELSE 0 END) AS ok_cnt, "
            "SUM(CASE WHEN COALESCE(has_conflict,0)=1 THEN 1 ELSE 0 END) AS conflict_cnt, "
            "SUM(CASE WHEN COALESCE(has_conflict,0)=0 AND COALESCE(is_resolved,0)=0 "
            "AND COALESCE(is_simulated,0)=0 AND COALESCE(is_stale,0)=0 "
            "THEN 1 ELSE 0 END) AS pending_cnt, "
            "SUM(CASE WHEN COALESCE(is_stale,0)=1 THEN 1 ELSE 0 END) AS stale_cnt, "
            "SUM(CASE WHEN COALESCE(is_simulated,0)=1 THEN 1 ELSE 0 END) AS simulated_cnt, "
            "SUM(LENGTH(COALESCE(content,''))) AS chars "
            "FROM global_facts "
            "WHERE scheme_id=? OR (project_id=? AND (scheme_id='' OR scheme_id IS NULL)) "
            "GROUP BY gt ORDER BY gt", (scheme_id, project_id or ""))
        for r in (await cur.fetchall() or []):
            gt = r["gt"] or "其他事实"
            ok_cnt = r["ok_cnt"] or 0
            if ok_cnt:
                inv.entries.append(FieldEntry(
                    source=SRC_FACTS, key=gt, label=gt, chars=r["chars"] or 0))
            for cnt, status, note in (
                    (r["conflict_cnt"] or 0, "filtered_conflict", "存在未裁决矛盾，按红线不注入"),
                    (r["pending_cnt"] or 0, "filtered_unresolved", "未经人工确认，按红线不注入"),
                    (r["stale_cnt"] or 0, "filtered_stale", "来源资料已变化，按红线不注入"),
                    (r["simulated_cnt"] or 0, "filtered_simulated", "AI 编造值，按红线不注入")):
                if cnt:
                    inv.entries.append(FieldEntry(
                        source=SRC_FACTS, key=f"{gt}#{status}", label=gt,
                        chars=0, status=status, note=f"{cnt} 条事实被过滤：{note}"))
    except Exception as e:
        inv.degraded.append(SRC_FACTS)
        logger.warning("盘点全局事实字段清单失败（降级跳过该源）: %s", e)

    # ---- 3. 方案名称施工内容 + 编制要求 ----
    for frag in (scope_items or []):
        inv.entries.append(FieldEntry(source=SRC_SCOPE, key=frag, label=frag))
    if (requirements_text or "").strip():
        inv.entries.append(FieldEntry(
            source=SRC_REQ, key="requirements", label="编制要求",
            chars=len(requirements_text.strip())))

    # ---- 4. 方案名称解析出的工序 / 工艺 / 对象（2026-09-26 新增） ----
    # 按维度分条登记，key 形如 "process:开挖"，便于差集报告精确定位
    # 「哪个维度没进提示词」；解析不出（名称无该维度的字面信息）则不登记
    # —— 与「不得编造」一致：清单里不该出现来源并不存在的字段。
    if basis is not None:
        for dim, items in (("process", getattr(basis, "process_steps", None) or []),
                           ("technique", getattr(basis, "techniques", None) or []),
                           ("object", getattr(basis, "objects", None) or [])):
            for it in items:
                inv.entries.append(FieldEntry(
                    source=SRC_BASIS, key=f"{dim}:{it}", label=it, chars=len(it)))
    if (standards_text or "").strip():
        inv.entries.append(FieldEntry(
            source=SRC_STANDARDS, key="standards", label="编制依据规范",
            chars=len(standards_text.strip())))
    return inv


def audit_prompt_coverage(inventory: InputInventory, prompt_text: str,
                         truncated_sources: dict[str, str] | None = None
                         ) -> dict:
    """字段清单 × 装配提示词 → 已调用/缺失/截断 对照表（差集分析，纯函数）。

    「已调用」锚点判定（与渲染器约定同源，宁漏报不误报）：
    - 解析提取：``## {label}``（format_downstream_context 的小节标题）；
    - 全局事实：``### {分组标题}``（_render_facts_text 的分组标题）；
    - 施工内容/编制要求：label 原文出现在提示词中。

    truncated_sources: {source: 截断说明}，由调用方在装配提示词时传入
    （如 {"解析提取": "project_brief 预算 4000 字已截断"}），审计只负责透传
    进报告，不猜测截断。
    """
    text = prompt_text or ""
    truncated_sources = truncated_sources or {}
    degraded = list(getattr(inventory, "degraded", []) or [])
    report: dict = {"injected": [], "missing": [], "not_injectable": [],
                    "degraded": degraded,
                    "truncated": [{"source": s, "reason": r}
                                  for s, r in truncated_sources.items()]}
    for e in inventory.entries:
        rec = {"source": e.source, "key": e.key, "label": e.label,
               "chars": e.chars, "status": e.status}
        if e.status != "available":
            rec["note"] = e.note or ("无有效提取内容" if e.status == "empty" else "被过滤")
            report["not_injectable"].append(rec)
            continue
        if e.source == SRC_PARSE:
            hit = f"## {e.label}" in text
        elif e.source == SRC_FACTS:
            hit = f"### {e.label}" in text
        else:
            hit = e.label in text
        (report["injected"] if hit else report["missing"]).append(rec)
    report["summary"] = (
        f"字段清单 {len(inventory.entries)} 项：已调用 {len(report['injected'])}、"
        f"缺失 {len(report['missing'])}、不可注入 {len(report['not_injectable'])}、"
        f"截断源 {len(report['truncated'])}"
        + (f"、降级源 {len(degraded)}({'、'.join(degraded)})" if degraded else ""))
    return report


def log_coverage_audit(scene: str, scheme_id: str, report: dict) -> None:
    """把差集报告写入日志：缺失/截断为 WARNING（可恢复异常），全绿为 INFO。

    日志是「完整调用」承诺的落地证据：每条缺失都带 source/key，运维可据此
    回溯到具体数据源与注入点。
    """
    try:
        missing = report.get("missing") or []
        truncated = report.get("truncated") or []
        degraded = report.get("degraded") or []
        if not missing and not truncated and not degraded:
            logger.info("输入依据差集审计[%s] 方案 %s：%s",
                        scheme_id[:8], scene, report.get("summary", ""))
            return
        detail = "; ".join(
            f"{m['source']}/{m['label']}" for m in missing[:20]) or "无"
        trunc = "; ".join(f"{t['source']}:{t['reason']}" for t in truncated) or "无"
        # ✅ m3：某源查询异常被降级跳过时，清单不完整不能当“通过”，同样报 WARNING。
        degr = "、".join(degraded) if degraded else "无"
        logger.warning(
            "输入依据差集审计[%s] 方案 %s 存在未调用/截断/降级：%s｜缺失=%s｜截断=%s｜降级源=%s",
            scene, scheme_id[:8], report.get("summary", ""), detail, trunc, degr)
    except Exception:
        # 审计日志绝不允许影响生成主流程
        pass
