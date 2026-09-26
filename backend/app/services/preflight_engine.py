"""程序化预检引擎（离线、秒级，无需 AI）

定位
----
与「AI 语义判定」互补，构成审核与预检的两条腿：

- **本引擎**：确定性规则，纯本地计算，秒级返回，可离线跑、可回归测试，
  结果稳定可复现（同一份正文永远得到同一份结论）。
- **AI 判定**：覆盖需要语义理解的部分（措施是否有针对性、参数取值依据等），
  见 ``routers/compliance.py`` 的 ``/check`` 与 ``/consistency-audit``。

商业级预检工具（对标 OpenBidKit 的 precheck）的关键特征之一就是
**"大部分问题不花钱也能查出来"**：空章节、废止标准、控制字符、查重、
计算书缺失这类硬伤用规则判定既快又准，不该消耗一次大模型调用。
只有真正需要语义判断的条目才交给 AI。

补齐的 PRD 遗漏项
-----------------
- §3.10.3 控制字符检查 → ``DLV-05``
- §3.10.3 计算书缺失检查 → ``TRC-01`` / ``CMP-09``
- §3.12.4 查重检查（相似度 > 80%）→ ``CON-05``

输出契约
--------
每条发现（finding）为统一结构，便于前端统一渲染与评分引擎聚合：

    {
      "rule_id": "STD-01",
      "dimension": "compliance",
      "severity": "block",
      "title": "...",
      "detail": "...",
      "evidence": [...],
      "section_id": "",      # 可空（全局性问题）
      "section_title": "",   # 可空
      "suggestion": "...",
      "basis": "行业依据"
    }
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.services.audit_rules import RULE_VERSION, SEVERITY_ORDER, get_rule
from app.services.content_polish import find_colloquial_hits
from app.services.content_utils import find_unclosed_fences
from app.services.standards_registry import (
    ABOLISHED_STANDARDS, CATEGORY_STANDARDS, find_abolished_codes,
    is_known_standard, match_categories, normalize_standard_code,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 低字数叶子章节阈值（与 export.py::export_check 保持一致，避免两处口径漂移）
LOW_WORD_THRESHOLD = 100
#: 参与查重的最小章节字数（过短章节相似度无统计意义）
DUP_MIN_WORDS = 200
#: 查重相似度阈值（《产品需求文档》§3.12.4）
DUP_SIMILARITY_THRESHOLD = 0.8
#: 近乎完全一致阈值（达到此值按"重复生成"升级为 high）
DUP_IDENTICAL_THRESHOLD = 0.95
#: 参与查重的章节数上限（O(n²) 保护）
DUP_MAX_SECTIONS = 80
#: 中文文本 shingle 长度（4-gram）
SHINGLE_SIZE = 4
#: 整体篇幅达标比例（总字数 / 字数预算）
WORD_BUDGET_OK_RATIO = 0.8

#: 图表"已完成"状态口径（与 export.py 保持一致）
CHART_DONE_STATUSES = ("done", "generated")

# 标准编号提取（GB / JGJ / JTG / DBJ / CECS / CJJ 等，含年号）
_STANDARD_CODE_RE = re.compile(
    r"(?:GB/T|GB|JGJ/T|JGJ|JTG/T|JTG|DBJ|CECS|CJJ/T|CJJ)\s*"
    r"[0-9]{2,5}(?:\.[0-9]{1,3})?(?:[—\-－]\s*[0-9]{4})?",
    re.IGNORECASE,
)
# 全文强制性工程建设规范（GB 55xxx 系列）
_MANDATORY_CODE_RE = re.compile(r"GB\s*55[0-9]{3}(?:[—\-－]\s*[0-9]{4})?", re.IGNORECASE)

# 计算过程特征：公式、等号计算式、参数代入
_FORMULA_RES = (
    re.compile(r"\$\$?[^$\n]{2,200}\$\$?"),                     # LaTeX
    re.compile(r"\\\(.{2,200}?\\\)|\\\[.{2,400}?\\\]"),          # \( \) \[ \]
    re.compile(r"[α-ωΑ-ΩA-Za-z_]{1,12}\s*=\s*[^=；;\n]{1,80}\d"),  # 变量 = 数值
    re.compile(r"(?:承载力|抗倾覆|抗滑移|稳定系数|安全系数|弯矩|剪力|挠度|应力)"
               r"[^。\n]{0,20}(?:计算|验算|校核)"),
)
_CALC_KEYWORDS = ("计算书", "验算", "受力计算", "承载力计算", "稳定性验算", "安全系数")

# 控制字符：C0（除 \t \n \r）+ C1 + 替换字符 U+FFFD
_CTRL_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffd\ufffe\uffff]")

# 数值主题一致性（与 consistency_scanner 保持同一口径，避免两处正则分叉）
# ✅ BUG 修复（2026-09-18）：主题定义升级为
#    (topic, pattern, value_groups, object_group)
#    —— object_group 非 None 表示「同主题不同对象各自成立」，必须先按对象分组、
#    再在组内判不同值。旧实现把「设备名 + 数量」整体拼成 key，于是
#    「塔吊 2 台 + 施工电梯 2 台」被判为「出现 2 种不同取值」→ CON-01（high）
#    误报 + consistency 维度扣 20 分，正常列出多种设备的方案必然中招。
#    「混凝土强度」同理：柱 C40 与梁 C30 属不同构件设计值，不是矛盾。
_NUM_TOPICS: tuple[tuple[str, re.Pattern, tuple[int, ...], int | None], ...] = (
    ("工期", re.compile(
        r"(?:总工期|工期|建设工期|施工工期)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(日历天|天|个月|月)"),
     (0, 1), None),
    ("基坑深度", re.compile(
        r"(?:开挖|基坑)[^。\n]{0,8}深度[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m|米|M)"),
     (0,), None),
    ("搭设高度", re.compile(
        r"(?:支撑|脚手架|支架|模板)[^。\n]{0,8}高度[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m|米|M)"),
     (0,), None),
    # 可选「构件/部位」前缀作为对象标识（捕获组 1），强度等级为组 2。
    # 前缀用非贪婪 `{0,6}?` 让引擎回溯，兼容「柱混凝土强度等级为C40」
    # 「梁的混凝土强度等级C30」两种写法。
    # 注意：下标与 m.group(i) 一致（1 = 第一个捕获组），m.group(0) 是整体匹配。
    ("混凝土强度", re.compile(
        r"((?:柱|梁|板|墙|基础|承台|桩|楼板|底板|顶板)[^0-9，。；、\n]{0,6}?)?"
        r"\s*(?:混凝土强度等级|混凝土为|砼)\s*[:：]?\s*C(\d{2,3})"),
     (2,), 1),
    # 设备名（组 1）= 对象标识；数量 + 单位（组 2、3）= 取值
    ("设备数量", re.compile(
        r"(塔吊|塔式起重机|施工电梯|升降机|挖掘机|起重机)[^0-9]{0,10}(\d+)\s*(台|套)"),
     (2, 3), 1),
)


@dataclass
class PreflightContext:
    """预检输入（由路由层从 DB 装配，便于单测直接构造）。"""

    scheme_id: str = ""
    scheme_name: str = ""
    scheme_type: str = ""
    word_budget: int = 0
    sections: list = field(default_factory=list)   # id/title/content/word_count/parent_id
    charts: list = field(default_factory=list)     # chart_type/status

    @property
    def body_text(self) -> str:
        return "\n\n".join((s.get("content") or "") for s in self.sections)


def _finding(rule_id: str, detail: str, *, evidence=None, section_id: str = "",
             section_title: str = "", suggestion: str = "") -> dict:
    """构造一条发现（自动补全规则的维度 / 严重度 / 标题 / 行业依据）。"""
    rule = get_rule(rule_id)
    return {
        "rule_id": rule_id,
        "dimension": rule.dimension if rule else "",
        "severity": rule.severity if rule else "medium",
        "title": rule.title if rule else rule_id,
        "detail": detail,
        "evidence": list(evidence or [])[:20],
        "section_id": section_id,
        "section_title": section_title,
        "suggestion": suggestion or (rule.detail if rule else ""),
        "basis": rule.basis if rule else "",
        "mode": "program",
    }


def _has_calc_process(text: str) -> bool:
    """判断文本是否含可识别的计算过程。"""
    if not text:
        return False
    return any(rx.search(text) for rx in _FORMULA_RES)


def _shingles(text: str) -> set:
    """中文 4-gram 集合（剔除空白与标点，降低格式差异带来的噪声）。"""
    cleaned = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith(("Z", "P", "S", "C"))
    )
    if len(cleaned) < SHINGLE_SIZE:
        return set()
    return {cleaned[i:i + SHINGLE_SIZE] for i in range(len(cleaned) - SHINGLE_SIZE + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


# ---------------------------------------------------------------------------
# 一、内容完整性（CMP-*）：专项方案九项法定内容
# ---------------------------------------------------------------------------
def check_completeness(ctx: PreflightContext) -> list:
    """检查住建部令第37号第十七条规定的九项内容是否齐备（按章节标题关键词匹配）。"""
    findings: list = []
    titles = [(s.get("title") or "") for s in ctx.sections]
    # ✅ BUG 修复（2026-09-22）：同名章节是合法存在的（各章都有“施工准备”/
    #    “安全保证措施”等），旧实现用 dict 按标题存正文，同名后者覆盖前者 →
    #    只要最后一个同名章节正文为空，整条规则就被误报“存在章节但正文为空”。
    #    现改为按标题聚合全部同名正文，任一非空即视为有内容。
    contents_by_title: dict = {}
    for t, s in zip(titles, ctx.sections):
        contents_by_title.setdefault(t, []).append(s.get("content") or "")

    for idx in range(1, 10):
        rule_id = f"CMP-{idx:02d}"
        rule = get_rule(rule_id)
        if not rule or not rule.keywords:
            continue
        hit_titles = [t for t in titles if any(kw in t for kw in rule.keywords)]
        if not hit_titles:
            findings.append(_finding(
                rule_id,
                f"未找到与「{rule.title}」相关的章节（期望标题含：{'/'.join(rule.keywords[:3])}）",
                suggestion=f"建议增设「{rule.keywords[0]}」章节，{rule.detail}",
            ))
            continue
        if not any((c or "").strip() for t in hit_titles for c in contents_by_title.get(t) or []):
            findings.append(_finding(
                rule_id, f"存在「{hit_titles[0]}」章节，但正文为空",
                section_title=hit_titles[0],
                suggestion="请在「正文生成」页生成该章节内容后重新预检",
            ))

    # CMP-09 追加：有计算书章节但无实际计算过程 → "有标题无实质"
    calc_titles = [t for t in titles if any(k in t for k in _CALC_KEYWORDS)]
    if calc_titles and not _has_calc_process(
            "\n\n".join(c for t in calc_titles
                         for c in contents_by_title.get(t) or [])):
        findings.append(_finding(
            "CMP-09",
            f"「{calc_titles[0]}」章节未见可识别的计算过程（无公式、无参数代入、无验算步骤）",
            section_title=calc_titles[0],
            suggestion="计算书应给出计算简图说明、参数取值、计算公式与结论"
                       "（建办质〔2018〕31号论证要点②）",
        ))
    return findings


# ---------------------------------------------------------------------------
# 二、规范符合性（STD-*）
# ---------------------------------------------------------------------------
def check_standards(ctx: PreflightContext) -> list:
    """检查正文引用的标准编号是否现行有效、是否覆盖全文强制规范与法定依据。"""
    findings: list = []
    text = ctx.body_text
    if not text.strip():
        return findings

    # ✅ BUG 修复（2026-09-18）：旧实现用「带空格的裸子串」匹配，正文写成
    #    "GB50202-2002"（无空格）或"GB 50202—2002"（全角破折号）时 STD-01 漏判
    #    （block 级红线失效）。现复用 standards_registry.find_abolished_codes 的
    #    归一化匹配，与质量自检同口径。
    abolished = find_abolished_codes(text)
    if abolished:
        sug = "；".join(f"{c} → {ABOLISHED_STANDARDS[c]}" for c in abolished[:5])
        findings.append(_finding(
            "STD-01", f"正文引用了 {len(abolished)} 个已废止 / 已被替代的标准编号",
            evidence=abolished, suggestion=f"请替换为现行版本：{sug}"))

    codes = {m.group(0).strip() for m in _STANDARD_CODE_RE.finditer(text)}

    if not any(_MANDATORY_CODE_RE.search(c) for c in codes):
        findings.append(_finding(
            "STD-02", "正文未引用任何全文强制性工程建设规范（GB 55xxx 系列）",
            suggestion="施工安全与质量控制相关章节应引用 GB 55034-2022 / GB 55032-2022 等全文强制规范"))

    unknown = sorted(c for c in codes if not is_known_standard(c))
    if len(unknown) >= 3:
        findings.append(_finding(
            "STD-03",
            f"检出 {len(unknown)} 个未收录于现行标准库的编号，请人工核实其编号与年号是否真实有效",
            evidence=unknown,
            suggestion="确需引用清单外标准时，必须同时给出完整标准编号与现行年号"))

    if not any(k in text for k in ("住建部令第37号", "37号令", "建办质〔2018〕31号",
                                   "危险性较大的分部分项工程安全管理规定")):
        findings.append(_finding(
            "STD-04", "正文未引用危大工程安全管理相关法规（住建部令第37号 / 建办质〔2018〕31号）",
            suggestion="编制依据章节应列明上述法定依据"))

    cats = match_categories(ctx.scheme_name, ctx.scheme_type)[:2]
    missing: list = []
    # ✅ 修复：同样按归一化文本比对，正文写 "GB55034-2022"（无空格）不该被误判为
    #    "未引用该类现行标准"。
    _norm_text = normalize_standard_code(text)
    for cat in cats:
        cat_codes = [s.code for s in CATEGORY_STANDARDS.get(cat, [])]
        if cat_codes and not any(
                normalize_standard_code(c) in _norm_text for c in cat_codes):
            missing.append(cat)
    if missing:
        findings.append(_finding(
            "STD-05", f"本方案属于「{'/'.join(missing)}」类，但未引用该类现行专业技术标准",
            suggestion="请在编制依据中补充对应专业技术标准（见现行标准库专业类别清单）"))
    return findings


# ---------------------------------------------------------------------------
# 三、安全措施有效性（SAF-*）
# ---------------------------------------------------------------------------
def check_safety(ctx: PreflightContext) -> list:
    """检查应急预案三要素与监测方案（GB/T 29639-2020）。"""
    findings: list = []
    emer = [s for s in ctx.sections
            if any(k in (s.get("title") or "") for k in ("应急", "救援", "预案"))]
    emer_text = "\n".join((s.get("content") or "") for s in emer)
    if emer and emer_text.strip():
        emer_title = emer[0].get("title") or ""
        for rule_id, kws, what in (
            ("SAF-03", ("组织机构", "应急组织", "领导小组", "指挥"), "应急组织机构及职责"),
            ("SAF-04", ("物资", "装备", "器材", "储备"), "应急物资装备保障"),
            ("SAF-05", ("演练", "演习"), "应急预案演练要求"),
        ):
            if not any(k in emer_text for k in kws):
                findings.append(_finding(
                    rule_id, f"应急处置章节缺少「{what}」相关内容",
                    section_title=emer_title,
                    suggestion=f"按 GB/T 29639-2020 要求补充{what}"))

    monitor_cats = {"基坑", "模板", "起重机械", "脚手架"}
    cats = set(match_categories(ctx.scheme_name, ctx.scheme_type))
    if cats & monitor_cats:
        mon = [s for s in ctx.sections
               if any(k in (s.get("title") or "") for k in ("监测", "监控", "变形", "沉降"))]
        if not mon:
            findings.append(_finding(
                "SAF-06",
                f"本方案属「{'/'.join(sorted(cats & monitor_cats))}」类危大工程，"
                "但未检出监测监控方案章节",
                suggestion="应明确监测项目、点位布置、监测频次与预警值"))
        elif not any(k in "".join((s.get("content") or "") for s in mon)
                     for k in ("预警", "报警", "控制值", "限值")):
            findings.append(_finding(
                "SAF-06", "监测方案未给出预警值 / 控制值",
                section_title=mon[0].get("title") or "",
                suggestion="监测方案应明确预警值与报警值，否则无法指导现场处置"))
    return findings


# ---------------------------------------------------------------------------
# 四、全文一致性（CON-*）
# ---------------------------------------------------------------------------
# ✅ 修复（2026-09-17）：数值一致性判定前先归一化单位，避免「90日历天」与「90天」、
#    「5m」与「5米」被误判为两种取值而误报 CON-01（high）。按「长串优先」顺序替换，
#    保证 cm/km/mm 等不被 m 误伤。
_UNIT_CANON: list[tuple[str, str]] = [
    ("日历天", "天"), ("自然天", "天"),
    ("km", "千米"), ("cm", "厘米"), ("mm", "毫米"),
    ("m²", "平方米"), ("m2", "平方米"), ("m", "米"),
    ("平方米", "平方米"),
    ("小时", "时"), ("h", "时"),
]
def _norm_unit(text: str) -> str:
    s = (text or "").strip()
    for a, b in _UNIT_CANON:
        s = s.replace(a, b)
    return s


def check_consistency(ctx: PreflightContext) -> list:
    """数值 / 术语一致性 + 章节查重。"""
    findings: list = []
    for topic, rx, val_groups, obj_group in _NUM_TOPICS:
        values: dict = {}
        by_object: dict = {}
        for s in ctx.sections:
            for m in rx.finditer(s.get("content") or ""):
                # 只拼接「取值」捕获组（对象标识组除外），避免 group 越界；
                # 归一化单位后再作为一致性判定的 key，避免单位写法差异造成误报。
                key = _norm_unit(
                    "".join((m.group(i) or "") for i in val_groups
                            if i <= rx.groups).strip())
                if not key:
                    continue
                if obj_group is None:
                    values.setdefault(key, s.get("title") or "")
                else:
                    obj = (m.group(obj_group) or "").strip() or "（未指明对象）"
                    by_object.setdefault(obj, {}).setdefault(key, s.get("title") or "")

        conflict_label, conflict_values = "", {}
        if obj_group is None:
            if len(values) > 1:
                conflict_label, conflict_values = topic, values
        else:
            # ✅ 按对象分组后组内判冲突：不同设备/构件各有各的数量，互不矛盾
            for obj, d in by_object.items():
                if len(d) > 1:
                    conflict_label = f"{topic}（{obj}）"
                    conflict_values = d
                    break
        if conflict_values:
            findings.append(_finding(
                "CON-01",
                f"「{conflict_label}」在正文中出现 {len(conflict_values)} 种不同取值",
                evidence=[f"{k}（{v}）"
                          for k, v in list(conflict_values.items())[:6]],
                suggestion=f"请统一{topic}口径；以全局事实 / 设计文件为准"))
            break  # 同一批次只报一个主题，避免刷屏

    # ✅ BUG 修复（2026-09-21）：旧实现所有重复章节对共用 rule_id="CON-05"，
    #    audit_scoring.merge_findings 按 rule_id 去重后只保留严重度最高的一条，
    #    导致「A vs B」「A vs C」「B vs D」多对重复内容在评分里只算一次问题，
    #    严重低估交付风险。现按出现顺序编号（CON-05-1, CON-05-2, ...），
    #    每对重复都是独立的交付缺陷。
    for _idx, dup in enumerate(_find_duplicates(ctx.sections), 1):
        # 近乎完全相同的两章（≥95%）几乎必然是生成重复或复制粘贴遗留，
        # 属"评审一眼看穿"的硬伤，升级为 high
        f = _finding(
            f"CON-05-{_idx}",
            f"「{dup['a_title']}」与「{dup['b_title']}」内容相似度 {dup['similarity']:.0%}",
            section_title=dup["a_title"], section_id=dup["a_id"] or "",
            suggestion="两章内容高度雷同，建议合并或分别聚焦不同工艺/部位，避免评审质疑")
        if dup["similarity"] >= DUP_IDENTICAL_THRESHOLD:
            f["severity"] = "high"
            f["detail"] += "（近乎完全一致，疑似重复生成）"
        findings.append(f)
    return findings


def _find_duplicates(sections: list) -> list:
    """章节两两相似度检测（中文 4-gram Jaccard）。"""
    candidates = [s for s in sections
                  if (s.get("word_count") or 0) >= DUP_MIN_WORDS
                  and (s.get("content") or "").strip()]
    if len(candidates) < 2:
        return []
    candidates.sort(key=lambda s: s.get("word_count") or 0, reverse=True)
    candidates = candidates[:DUP_MAX_SECTIONS]
    shingles = [(s, _shingles(s.get("content") or "")) for s in candidates]

    out: list = []
    for i, (si, sa) in enumerate(shingles):
        for sj, sb in shingles[i + 1:]:
            sim = _jaccard(sa, sb)
            if sim >= DUP_SIMILARITY_THRESHOLD:
                out.append({
                    "a_id": si.get("id"), "a_title": si.get("title"),
                    "b_id": sj.get("id"), "b_title": sj.get("title"),
                    "similarity": round(sim, 3),
                })
    out.sort(key=lambda x: x["similarity"], reverse=True)
    return out[:10]


# ---------------------------------------------------------------------------
# 五、可追溯性（TRC-*）
# ---------------------------------------------------------------------------
def check_traceability(ctx: PreflightContext) -> list:
    """计算书与验算依据（建办质〔2018〕31号论证要点②）。"""
    findings: list = []
    titles = [(s.get("title") or "") for s in ctx.sections]

    calc_secs = [s for s in ctx.sections
                 if any(k in (s.get("title") or "") for k in _CALC_KEYWORDS)]
    if not calc_secs:
        # ⚠️ 不在此处重复报"完全缺失" —— 该情形已由 CMP-09（内容完整性维度）以
        #    阻断级报告。同一缺陷在两个维度各扣一次 40 分会让总分失真，
        #    本条只负责"个别章节缺计算过程"这一独立缺陷面。
        pass
    else:
        no_calc = [s for s in calc_secs if not _has_calc_process(s.get("content") or "")]
        # ✅ 跨维度双扣修复（2026-09-23）：当**全部**计算书章节都无计算过程时，
        #    CMP-09 已按「聚合口径」以 block 级报同一缺陷（-40），此处再逐章报
        #    high（-20）即同一缺陷双扣 60。TRC-01 改为只在"整体有过程、个别章节
        #    缺失"时报告（与 CMP-09 严格互斥：聚合文本集与 CMP-09 追加条款同源，
        #    均为标题命中 _CALC_KEYWORDS 的全部章节，「聚合无过程 ⇔ CMP-09 必报」）。
        if no_calc and _has_calc_process(
                "\n\n".join(s.get("content") or "" for s in calc_secs)):
            findings.append(_finding(
                "TRC-01", f"「{no_calc[0].get('title')}」未见计算公式或参数代入过程",
                section_title=no_calc[0].get("title") or "",
                section_id=no_calc[0].get("id") or "",
                suggestion="计算书须包含：计算依据、参数取值及其来源、计算过程、结论与安全系数"))

    if not any(any(k in t for k in ("附图", "详图", "平面布置", "节点图", "图纸"))
               for t in titles):
        findings.append(_finding(
            "TRC-03", "未检出附图 / 节点详图相关章节或引用",
            suggestion="专项方案应附相关图纸（平面布置、节点详图、监测点布置等）"))
    return findings


# ---------------------------------------------------------------------------
# 六、可交付性（DLV-*）
# ---------------------------------------------------------------------------
def check_deliverability(ctx: PreflightContext) -> list:
    """空章节 / 字数 / 孤立节点 / 图表 / 控制字符 / 口语化 / 围栏 / 篇幅。"""
    findings: list = []
    sections = ctx.sections
    section_ids = {s.get("id") for s in sections}
    parent_ids = {s.get("parent_id") for s in sections if s.get("parent_id")}

    empty: list = []
    low_word: list = []
    orphan: list = []
    ctrl_hits: list = []
    colloquial: list = []
    fence_bad: list = []
    total_words = 0

    for s in sections:
        title = s.get("title") or ""
        content = s.get("content") or ""
        wc = s.get("word_count") or 0
        total_words += wc
        has_children = s.get("id") in parent_ids

        if not has_children and not content.strip():
            empty.append(title)
        if not has_children and 0 < wc < LOW_WORD_THRESHOLD:
            low_word.append(f"{title}（{wc} 字）")
        pid = s.get("parent_id") or ""
        if pid and pid not in section_ids:
            orphan.append(title)
        if content:
            ctrl = _CTRL_CHARS_RE.findall(content)
            if ctrl:
                ctrl_hits.append({"title": title, "count": len(ctrl)})
            hits = find_colloquial_hits(content)
            if hits:
                colloquial.append({"title": title, "hits": hits[:5]})
            # ✅ 修复（2026-09-22）：旧实现用 content.count("```") % 2 == 1 判奇偶，
            # 存在两类漏报/误报：① 只认 ``` 围栏，漏掉 ~~~ 波浪围栏；② 把正文/行内
            # 出现的 ```（如"代码 `a` 与 ``` 长串"）误算，导致无围栏章节被错判为未闭合。
            # 现改用 CommonMark 口径的 find_unclosed_fences：逐行扫描、围栏内部不解析
            # 新围栏、闭合标记须同种且长度不小于开围栏，对 ``` 与 ~~~ 一视同仁。
            if find_unclosed_fences(content):
                fence_bad.append(title)

    if empty:
        findings.append(_finding(
            "DLV-01", f"{len(empty)} 个叶子章节为空", evidence=empty,
            suggestion="请在「正文生成」页补全生成，或删除多余空章节"))
    if low_word:
        findings.append(_finding(
            "DLV-02", f"{len(low_word)} 个章节字数不足 {LOW_WORD_THRESHOLD} 字",
            evidence=low_word, suggestion="可使用「扩写」补充内容，或调整该章节字数预算"))
    if orphan:
        findings.append(_finding(
            "DLV-03", f"{len(orphan)} 个章节父节点不存在（孤立节点）",
            evidence=orphan, suggestion="孤立节点会导致目录编号错乱，请重新挂载或删除"))

    total_charts = len(ctx.charts)
    if total_charts:
        undone = [c.get("chart_type") or "?" for c in ctx.charts
                  if (c.get("status") or "") not in CHART_DONE_STATUSES]
        if undone:
            findings.append(_finding(
                "DLV-04", f"{len(undone)}/{total_charts} 个图表未完成生成",
                evidence=undone, suggestion="未生成的图表在交付文档中会缺图或显示占位"))

    if ctrl_hits:
        total_ctrl = sum(h["count"] for h in ctrl_hits)
        findings.append(_finding(
            "DLV-05", f"{len(ctrl_hits)} 个章节含控制字符 / 替换字符（共 {total_ctrl} 处）",
            evidence=[f"{h['title']}（{h['count']} 处）" for h in ctrl_hits],
            suggestion="控制字符导出后表现为乱码或方框，请清除后重新预检"))
    if colloquial:
        findings.append(_finding(
            "DLV-06", f"{len(colloquial)} 个章节残留口语化 / AI 腔表达",
            evidence=[f"{h['title']}：{'、'.join(h['hits'])}" for h in colloquial],
            suggestion="交付文本应为工程书面语，请改写为规范表述"))
    if fence_bad:
        findings.append(_finding(
            "DLV-07", f"{len(fence_bad)} 个章节存在未闭合的 Markdown 代码围栏",
            evidence=fence_bad, suggestion="未闭合围栏会吞噬后续正文，请补全 ``` 结束标记"))

    if ctx.word_budget and total_words < ctx.word_budget * WORD_BUDGET_OK_RATIO:
        findings.append(_finding(
            "DLV-08",
            f"方案总字数 {total_words} 字，低于字数预算 {ctx.word_budget} 字的 "
            f"{WORD_BUDGET_OK_RATIO:.0%}",
            suggestion="可使用「补全生成」扩充内容，或调整方案字数预算"))
    return findings


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_preflight(ctx: PreflightContext) -> list:
    """执行全部程序化规则，返回发现列表（按严重度降序）。

    单条规则抛错时记录并继续，不因一处异常导致整轮预检 500
    （预检是"体检"，某一体检项失败不应让用户拿不到任何结论）。
    """
    findings: list = []
    if not ctx.sections:
        # 零章节不是“没有问题”，而是交付物根本不存在。普通空叶子仍沿用
        # DLV-01 的 high 严重度；整个方案没有目录时升级为 block，禁止 A 级放行。
        findings.append({
            "rule_id": "DLV-01", "dimension": "deliverability", "severity": "block",
            "title": "方案未生成任何章节",
            "detail": "方案目录为空，无法执行内容完整性、规范符合性与交付形态检查",
            "evidence": [], "section_id": "", "section_title": "",
            "suggestion": "请先生成或保存完整目录，再执行正文生成与一键总检",
            "basis": "《危险性较大的分部分项工程安全管理规定》第十七条",
            "mode": "program",
        })
        return findings
    for checker in (
        check_completeness, check_standards, check_safety,
        check_consistency, check_traceability, check_deliverability,
    ):
        try:
            findings.extend(checker(ctx))
        except Exception as exc:  # pragma: no cover - 防御性兜底
            findings.append({
                "rule_id": "", "dimension": "", "severity": "low",
                "title": f"检查项 {checker.__name__} 执行异常",
                "detail": str(exc)[:200], "evidence": [], "section_id": "",
                "section_title": "", "suggestion": "请查看后端日志", "basis": "",
                "mode": "program",
            })
    # ✅ BUG 修复（2026-09-23）：按 rule_id 去重（保留首条，同严重度下信息已足够；
    #    若后续条严重度更高则替换，位置保持首次出现处）。背景：正文为空的计算书
    #    章节会被 CMP-09 双发（「正文为空」+「无计算过程」），score_findings 不去重
    #    → 独立 /preflight 双扣 40 分，而 /overview 经 merge_findings 按 rule_id
    #    去重只扣一次，同一内容两条链路分数不一致。其余规则经排查均为单条最多
    #    发一次（CON-05 已用 CON-05-N 后缀编号），去重不改变既有语义。
    #    异常兜底行 rule_id 为空串，不参与去重（保留全部，便于运维发现重复故障）。
    deduped: list = []
    index_by_rid: dict = {}
    for f in findings:
        rid = f.get("rule_id") or ""
        if not rid:
            deduped.append(f)
            continue
        pos = index_by_rid.get(rid)
        if pos is None:
            index_by_rid[rid] = len(deduped)
            deduped.append(f)
        elif SEVERITY_ORDER.get(f.get("severity"), 0) > \
                SEVERITY_ORDER.get(deduped[pos].get("severity"), 0):
            deduped[pos] = f
    return _sort_findings(deduped)


def _sort_findings(findings: list) -> list:
    """按严重度降序、规则号升序排序（前端表格默认顺序即处理优先级）。"""
    from app.services.audit_rules import SEVERITY_ORDER
    return sorted(
        findings,
        key=lambda f: (-SEVERITY_ORDER.get(f.get("severity"), 0),
                       f.get("rule_id") or "zzz"))


def preflight_stats(ctx: PreflightContext) -> dict:
    """与发现列表配套的客观统计（前端完整性看板用）。"""
    # ✅ 性能修复（2026-09-23）：旧实现把父集合推导写在列表推导的条件里，
    #    每个章节都重建一次集合（O(n²)）；现提到循环外只算一次。
    parent_ids = {x.get("parent_id") for x in ctx.sections if x.get("parent_id")}
    leaf = [s for s in ctx.sections if s.get("id") not in parent_ids]
    wc = sum(s.get("word_count") or 0 for s in ctx.sections)
    generated = sum(1 for s in ctx.sections
                    if (s.get("word_count") or 0) > 0 or (s.get("content") or "").strip())
    chart_done = sum(1 for c in ctx.charts
                     if (c.get("status") or "") in CHART_DONE_STATUSES)
    return {
        "section_count": len(ctx.sections),
        "leaf_count": len(leaf),
        "generated_count": generated,
        "total_words": wc,
        "word_budget": ctx.word_budget,
        "empty_ratio": round((len(ctx.sections) - generated) / max(len(ctx.sections), 1) * 100, 1),
        "chart_total": len(ctx.charts),
        "chart_done": chart_done,
        "standard_db_version": None,   # 由路由层填充（避免本模块依赖版本常量来源）
    }


__all__ = [
    "PreflightContext", "run_preflight", "preflight_stats", "RULE_VERSION",
    "LOW_WORD_THRESHOLD", "DUP_SIMILARITY_THRESHOLD",
]
