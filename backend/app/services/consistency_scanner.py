"""一致性扫描器（F-AGENT-CONSISTENCY-REPAIR §5.1）

流程：
1. 收集全部叶子节点正文（content 非空的末级章节）
2. 加载全局事实变量、项目资料/设计文件/规范摘要
3. 程序预扫描：正则提取工期/深度/强度/数量/人名/型号/规范号等线索
4. AI 分片提取跨章节冲突（按章节总量切片，控制上下文长度）
5. 合并去重，生成统一冲突清单（C001...），供仲裁器定级

扫描只读，不修改正文。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime

from app.config import settings
from app.services.ai.json_response import collect_json_response
from app.services.ai.prompts._registry import render
from app.services.ai.provider_factory import effective_batch_size
from app.services.content_utils import order_sections_dfs
from app.services.standards_registry import get_standards_text

logger = logging.getLogger("consistency_scanner")

# 单片章节正文总量上限（字符），超出则切多片
SECTION_CHUNK_LIMIT = 12000
# 单章截断上限
PER_SECTION_LIMIT = 6000

# ---------- P0-1（2026-09-22）：扫描批处理 / 并发 / 增量缓存 ----------
# 实测（mock 驱动 12 章全量正文生成）：收尾的全文一致性阶段占总调用 67.6%，
# 其中扫描恒为 N 次（逐章一次、且串行）、定向修复 ≈ 涉及章节数。三步优化：
#   ① 批处理：一次调用扫 k 章（调用数 N → ⌈N/k⌉），批结果不合法则按章回退；
#   ② 并发：批之间并发（旧实现纯串行 for），只降墙钟不降调用数；
#   ③ 增量：按「章节正文指纹 + 上下文指纹」缓存，改一章只重扫一章。
CONSISTENCY_SCAN_BATCH_SIZE = max(
    1, int(getattr(settings, "consistency_scan_batch_size", 1) or 1))
CONSISTENCY_SCAN_CONCURRENCY = max(
    1, int(getattr(settings, "consistency_scan_concurrency", 1) or 1))
# 单批正文字符上限（章数之外第二道闸门）：防止 prompt 过长导致超时 / JSON 截断
CONSISTENCY_SCAN_BATCH_CHARS = 18000

# ---------- 程序预扫描规则 ----------
_NUM_TOPICS = [
    ("工期", r"(?:总工期|工期|建设工期|施工工期)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(日历天|天|个月|月)"),
    ("基坑深度", r"(?:开挖|基坑)[^。\n]{0,8}深度[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m|米|M)"),
    ("搭设高度", r"(?:支撑|脚手架|支架|模板)[^。\n]{0,8}高度[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m|米|M)"),
    ("混凝土强度", r"((?:柱|梁|板|墙|基础|承台|桩|楼板|底板|顶板)[^0-9，。；、\n]{0,6}?)?\s*(?:混凝土强度等级|混凝土为|砼)\s*[:：]?\s*C(\d{2,3})"),
    ("质保期", r"(?:质保期|保修期|质量保修期)[^0-9]{0,10}(\d+)\s*(年|个月|月)"),
    ("响应时间", r"(?:响应(?:时间|时效)|到场(?:时间|时效))[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(小时|h|H)"),
    ("设备数量", r"(塔吊|塔式起重机|施工电梯|升降机|挖掘机|起重机)[^0-9]{0,10}(\d+)\s*(台|套)"),
]

#: 需要「按对象分组后再比较」的主题 → 对象标识所在捕获组下标（1 = 第一个捕获组，
#  与 m.group(i) 一致；m.group(0) 是整体匹配，不是捕获组）。
#  ✅ BUG 修复（2026-09-18）：设备数量/混凝土强度这类主题的取值必须**按对象**比较，
#  否则「塔吊 2 台 + 施工电梯 2 台」「柱 C40 + 梁 C30」会被当作跨章节数值矛盾，
#  对正常方案产生必然的误报（与 preflight_engine 的 CON-01 同一口径）。
_NUM_TOPIC_OBJECT_GROUP = {"设备数量": 1, "混凝土强度": 1}
#: 岗位简称与全称等价（避免同一岗位被拆成两桶而产生假冲突）
_ROLE_ALIASES = {"技术负责人": "项目技术负责人"}
#: 岗位后常见的「动词 / 连接词」开头片段 —— 这些不是人名，是谓语。
#:
#: ✅ BUG 修复（2026-10-02，生产数据实证）
#: -------------------------------------------
#: 旧正则第 2 个捕获组是「岗位后任意 2~4 个汉字」，于是
#: 「技术负责人**组织各专**业」「项目技术负责人**签发**」
#: 「现场负责人**接到报告**」里的谓语全被当成「人名」，
#: 再因「同一岗位出现多个不同人名」被判为跨章节矛盾。
#: 生产实证（consistency_conflicts C004/C005/C006 = CON-SCAN-4/5/6）：
#: 3 条 medium 冲突**全部**由本缺陷产生，捕获值是
#: 「组织各专 / 审核后归 / 批准后 / 签发 / 重排对应 / 接到报告 / 或专职安」。
#: 现按「谓语前缀」拦截；真正的中文人名不会以这些字开头。
_PERSON_NAME_STOP_PREFIX = (
    "组织", "审核", "审", "批准", "审批", "签发", "签字", "签署", "确认", "核实",
    "重排", "调整", "安排", "部署", "负责", "担任", "会同", "参加",
    "接到", "接报", "巡查", "检查", "监督", "验收", "复核", "报", "上报", "提交",
    "编制", "起草", "拟", "制定", "责令", "要求", "督促", "协调",
    "或", "和", "与", "及", "等", "该", "各", "本", "其", "并", "同时", "立即",
    "应当", "应", "必须", "须", "要", "将", "把", "对", "向", "由", "从",
)
#: 谓语的「兜底」判定：捕获值若整体落在常见公文动词集合内，同样不是人名。
_PERSON_NAME_STOP_EXACT = frozenset({
    "负责", "担任", "组织", "审核", "批准", "审批", "签发", "确认", "检查",
    "监督", "验收", "复核", "上报", "提交", "编制", "起草", "制定", "协调",
    "安排", "部署", "巡查", "参加", "会同", "接报", "接到", "责令", "要求",
})


def _looks_like_person_name(name: str) -> bool:
    """判定捕获到的片段是否像「人名」（用于剔除谓语短语造成的误报）。"""
    if not name:
        return False
    if name in _PERSON_NAME_STOP_EXACT:
        return False
    return not name.startswith(_PERSON_NAME_STOP_PREFIX)


_PERSON_RE = re.compile(
    r"(项目经理|项目技术负责人|技术负责人|现场负责人|安全负责人|总监理工程师)"
    r"\s*[:：为是]?\s*([\u4e00-\u9fa5]{2,4})(?![a-zA-Z0-9])")
_MODEL_RE = re.compile(
    r"\b(?:QTZ?\d{2,4}[A-Za-z]?|SC\d{3}|C\d{2}(?:/P\d)?|Q\d{3}[A-Z]?|HPB\d{3}|HRB\d{3})\b")
_STANDARD_RE = re.compile(r"(GB(?:/T)?|JGJ(?:/T)?|DBJ\d?|JTG(?:/T)?)\s*[0-9]{2,5}(?:\.\d+)?(?:[—\-－]\d{4})?")

_SEVERITY_BY_TYPE = {
    "numeric": "medium", "param": "medium", "person": "medium",
    "model": "medium", "timeline": "medium", "commitment": "medium",
    "duplication": "low", "facts": "high", "design": "high", "standard": "high",
}


def new_scan_id() -> str:
    return f"scan_{uuid.uuid4().hex[:12]}"


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]


async def build_global_facts_text(db, scheme_id: str, limit: int = 5000) -> str:
    """全局事实文本（复用 sse_handlers 的过滤口径：剔除矛盾值/未确认模拟值）。"""
    try:
        from app.routers.sse_handlers import _build_facts_text
        return await _build_facts_text(db, scheme_id, max_total=limit)
    except Exception as e:
        logger.warning("构建全局事实文本失败（降级为空）: %s", e)
        return ""


async def build_project_docs_text(db, project_id: str, *, doc_type: str = "",
                                  limit: int = 4000) -> str:
    """项目资料 / 设计文件摘要（project_documents.parsed_markdown 聚合截断）。"""
    try:
        sql = ("SELECT file_name, doc_type, parsed_markdown FROM project_documents "
               "WHERE project_id=?")
        params: list = [project_id]
        if doc_type:
            sql += " AND doc_type=?"
            params.append(doc_type)
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    except Exception as e:
        logger.warning("查询项目资料失败（降级为空）: %s", e)
        return ""
    parts: list[str] = []
    total = 0
    for file_name, dtype, markdown in rows:
        block = f"### {file_name or '未命名资料'}\n{_clip(markdown or '', 1500)}\n"
        if total + len(block) > limit:
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)[:limit]


def build_standards_text(scheme_name: str, scheme_type: str) -> str:
    try:
        return _clip(get_standards_text(scheme_name, scheme_type, max_categories=3), 3000)
    except Exception as e:
        logger.warning("构建规范摘要失败（降级为空）: %s", e)
        return ""


async def load_leaf_sections(db, scheme_id: str) -> list[dict]:
    """收集 content 非空的末级（叶子）章节。

    用「不存在 content 非空子节点」判定叶子；无层级信息时退化为全部有正文章节。
    """
    cur = await db.execute(
        "SELECT id, parent_id, title, content, level, sort_order FROM sections "
        "WHERE scheme_id=? AND IFNULL(content,'')!='' ORDER BY sort_order",
        (scheme_id,))
    # ✅ 遗留修复（2026-09-22）：sort_order 是同级序号非文档序，扁平排会造成
    #    扫描/AI 上下文章节次序错乱；重排为目录树前序 DFS（与正文生成同口径）。
    all_rows = [dict(r) for r in order_sections_dfs([dict(r) for r in await cur.fetchall()])]
    cur2 = await db.execute(
        "SELECT DISTINCT parent_id FROM sections WHERE scheme_id=? AND IFNULL(content,'')!=''",
        (scheme_id,))
    parent_ids = {r[0] for r in await cur2.fetchall() if r[0]}
    # 叶子 = 自身 id 不在「有正文节点的父集合」中的章节；
    # 退化情况（无层级信息）则退化为全部有正文章节。
    leaves = [r for r in all_rows if r["id"] not in parent_ids]
    return leaves or all_rows


def program_prescan(sections: list[dict]) -> list[dict]:
    """程序预扫描：对数值/人名/型号/规范号提取线索，同主题出现不同值时记为候选冲突。

    仅产出"跨章节同主题不同值"的强信号候选，降低 AI 漏检率；最终仍以 AI 仲裁为准。
    """
    # topic -> {value: [occurrence, ...]}
    buckets: dict[str, dict[str, list[dict]]] = {}
    for sec in sections:
        sid, title, content = sec["id"], sec["title"], sec["content"] or ""
        for topic, pattern in _NUM_TOPICS:
            obj_group = _NUM_TOPIC_OBJECT_GROUP.get(topic)
            for m in re.finditer(pattern, content):
                if obj_group is None:
                    value = "".join(g for g in m.groups() if g)
                    bucket_key = topic
                else:
                    # ✅ 按对象分组：不同设备/构件各有各的取值，不构成数值矛盾。
                    #    取值 = 除对象标识组（1-based）外的全部捕获组。
                    value = "".join(
                        (m.group(i) or "") for i in range(1, m.re.groups + 1)
                        if i != obj_group)
                    obj = (m.group(obj_group) or "").strip()
                    bucket_key = f"{topic}｜{obj}" if obj else topic
                if not value:
                    continue
                _add_bucket(buckets, bucket_key, value, sid, title, m.group(0))
        for m in _PERSON_RE.finditer(content):
            role, name = m.group(1), m.group(2)
            # ✅ BUG 修复（2026-10-02）：岗位简称/全称归一（见 _ROLE_ALIASES）。
            #    「技术负责人」与「项目技术负责人」是同一岗位，旧实现分成两个桶，
            #    各自因谓语被误当成不同「人名」而各报一条冲突。
            role = _ROLE_ALIASES.get(role, role)
            # 谓语短语不是人名（见 _looks_like_person_name 的生产实证）
            if not _looks_like_person_name(name):
                continue
            _add_bucket(buckets, role, name, sid, title, m.group(0))
        for m in _MODEL_RE.finditer(content):
            _add_bucket(buckets, "设备/材料型号", m.group(0), sid, title, m.group(0))

    candidates: list[dict] = []
    for topic, values in buckets.items():
        if len(values) < 2:
            continue
        occurrences = []
        for value, occs in values.items():
            occurrences.extend([{**o, "value": value} for o in occs])
        candidates.append({
            "conflict_type": "numeric",
            "topic": topic,
            "value": " / ".join(values.keys()),
            "text": "；".join(o["text"] for o in occurrences[:6]),
            "position": 0,
            "section_occurrences": occurrences,
            "source": "program_prescan",
        })
    return candidates


def _add_bucket(buckets: dict, topic: str, value: str, sid: str, title: str, text: str):
    value = (value or "").strip()
    if not value:
        return
    occ = {"section_id": sid, "section_title": title,
           "text": text[:120], "position": 0}
    buckets.setdefault(topic, {}).setdefault(value, [])
    if not any(o["section_id"] == sid for o in buckets[topic][value]):
        buckets[topic][value].append(occ)


async def ai_scan_section(*, section: dict, facts: str, project_docs: str,
                          design_docs: str, standards: str) -> list[dict]:
    """AI 分片提取单章冲突候选（低温 JSON）。失败返回空列表，绝不阻断扫描。"""
    user = render(
        "consistency_scan_user",
        global_facts=facts or "（无）",
        project_docs_summary=project_docs or "（无）",
        design_docs_summary=design_docs or "（无）",
        standards_summary=standards or "（无）",
        section_id=section["id"],
        section_title=section["title"],
        section_content=_clip(section["content"], PER_SECTION_LIMIT),
    )
    system = render("consistency_scan_system")
    try:
        obj, _ = await collect_json_response(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            lambda o: [] if isinstance(o.get("conflicts"), list) else ["缺少 conflicts"],
            max_retries=1, temperature=0.1, json_mode=True, timeout=180,
            scene="consistency_scan")
        rows = obj.get("conflicts") or []
    except Exception as e:
        logger.warning("章节 %s AI 扫描失败（跳过）: %s", section["title"], e)
        return []
    return normalize_scan_rows(rows, section)


def normalize_scan_rows(rows: list[dict], section: dict) -> list[dict]:
    """把 AI 返回的原始冲突行归一为统一的候选结构（单章 / 批扫描共用）。

    与缓存口径一致：增量缓存存的就是归一化后的行，命中后无需再加工。
    """
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("topic"):
            continue
        out.append({
            "conflict_type": r.get("conflict_type") or r.get("type") or "numeric",
            "topic": str(r.get("topic"))[:60],
            "value": str(r.get("value") or "")[:120],
            "text": str(r.get("text") or "")[:300],
            "position": int(r.get("position") or 0),
            "section_occurrences": [{
                "section_id": section["id"],
                "section_title": section["title"],
                "text": str(r.get("text") or "")[:300],
                "position": int(r.get("position") or 0),
                "value": str(r.get("value") or "")[:120],
            }],
            "source": "ai_scan",
        })
    return out


# ---------- P0-1 ①：多章合并批扫描 ----------
def _group_batch_rows(rows: list, sections: list[dict]) -> dict[str, list[dict]] | None:
    """把批扫描结果按 section_id 归组。

    ✅ 防线（与目录生成「绝不整批判死」同口径）：任何一条冲突无法归属到本批章节
    （缺 section_id / 指向批次外的 id）→ 整批作废返回 None，由调用方按章回退单章
    扫描。宁可多花几次调用，也不把归错章的冲突写进清单。
    """
    ids = {s["id"] for s in sections}
    out: dict[str, list[dict]] = {s["id"]: [] for s in sections}
    for r in rows:
        if not isinstance(r, dict):
            return None
        sid = str(r.get("section_id") or "").strip()
        if sid not in ids:
            return None
        out[sid].append(r)
    return out


async def ai_scan_batch(*, sections: list[dict], facts: str, project_docs: str,
                        design_docs: str, standards: str) -> dict[str, list[dict]] | None:
    """一次 AI 调用扫描多个章节，返回 {section_id: 原始冲突行}。

    返回 None 表示「本批不可用」（AI 调用失败或结构不合法），调用方按章回退。
    """
    block = "\n".join(
        f"章节ID：{sec['id']}\n章节标题：{sec['title']}\n章节内容：\n"
        f"{_clip(sec.get('content') or '', PER_SECTION_LIMIT)}\n-----"
        for sec in sections)
    user = render("consistency_scan_batch_user",
                  global_facts=facts or "（无）",
                  project_docs_summary=project_docs or "（无）",
                  design_docs_summary=design_docs or "（无）",
                  standards_summary=standards or "（无）",
                  section_count=len(sections), sections_block=block)
    system = render("consistency_scan_batch_system")
    try:
        obj, _ = await collect_json_response(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            lambda o: [] if isinstance(o.get("conflicts"), list) else ["缺少 conflicts"],
            max_retries=1, temperature=0.1, json_mode=True, timeout=240,
            scene="consistency_scan")
    except Exception as e:
        logger.warning("多章合并扫描失败（%d 章，按章回退）: %s", len(sections), e)
        return None
    grouped = _group_batch_rows(obj.get("conflicts") or [], sections)
    if grouped is None:
        logger.warning("多章合并扫描结果无法按章节归属（%d 章），按章回退", len(sections))
    return grouped


def chunk_sections_for_scan(sections: list[dict], *,
                            batch_size: int | None = None,
                            max_chars: int | None = None) -> list[list[dict]]:
    """按「章数 + 字符数」双上限切批（纯函数，可单测）。

    batch_size=1 时退化为「每章一批」，与旧行为逐字一致。
    """
    k = max(1, int(batch_size or CONSISTENCY_SCAN_BATCH_SIZE))
    limit = max(1, int(max_chars or CONSISTENCY_SCAN_BATCH_CHARS))
    out: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for sec in sections:
        n = min(len(sec.get("content") or ""), PER_SECTION_LIMIT)
        if cur and (len(cur) >= k or cur_chars + n > limit):
            out.append(cur)
            cur, cur_chars = [], 0
        cur.append(sec)
        cur_chars += n
    if cur:
        out.append(cur)
    return out


# ---------- P0-1 ③：增量缓存（章节正文指纹 + 上下文指纹） ----------
def content_fingerprint(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


def context_fingerprint(facts: str, project_docs: str,
                        design_docs: str, standards: str) -> str:
    """上下文指纹：事实/资料/设计文件/规范任一变化 → 缓存整体失效重新扫描。"""
    blob = "\n".join([facts or "", project_docs or "", design_docs or "", standards or ""])
    return hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()[:16]


async def _load_cached_rows(db, sections: list[dict],
                            ctx_hash: str) -> tuple[dict[str, list], list[dict]]:
    """返回 (命中表 section_id -> rows, 未命中章节列表)。

    缓存不可用（表缺失 / 查询报错 / JSON 损坏）时全部视为未命中 —— 增量只是优化，
    绝不因此让扫描失败或静默丢冲突。
    """
    if not sections:
        return {}, []
    try:
        marks = ",".join("?" * len(sections))
        cur = await db.execute(
            "SELECT section_id, content_hash, rows_json FROM consistency_scan_cache"
            f" WHERE section_id IN ({marks}) AND context_hash=?",
            (*[s["id"] for s in sections], ctx_hash))
        raw = {dict(r)["section_id"]: dict(r) for r in await cur.fetchall()}
    except Exception as e:
        logger.warning("一致性扫描缓存读取失败（本次全量重扫）: %s", e)
        return {}, list(sections)
    hit: dict[str, list] = {}
    pending: list[dict] = []
    for sec in sections:
        rec = raw.get(sec["id"])
        if rec is None:
            pending.append(sec)
            continue
        try:
            rows = json.loads(rec["rows_json"] or "[]")
        except Exception:
            pending.append(sec)
            continue
        if rec["content_hash"] != content_fingerprint(sec.get("content") or ""):
            pending.append(sec)  # 正文已变更 → 该章缓存失效
            continue
        hit[sec["id"]] = rows
    return hit, pending


async def _save_cached_rows(db, scheme_id: str, rows_by_sid: dict[str, list],
                            ctx_hash: str, sections_by_id: dict[str, dict]) -> None:
    """把本次扫描结果写入增量缓存（失败只告警，不影响本次结果）。"""
    payload = []
    for sid, rows in (rows_by_sid or {}).items():
        sec = sections_by_id.get(sid)
        if not sec:
            continue
        try:
            payload.append((sid, scheme_id,
                            content_fingerprint(sec.get("content") or ""),
                            ctx_hash, json.dumps(rows or [], ensure_ascii=False)))
        except Exception:
            continue
    if not payload:
        return
    try:
        await db.executemany(
            "INSERT INTO consistency_scan_cache"
            " (section_id, scheme_id, content_hash, context_hash, rows_json)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(section_id) DO UPDATE SET"
            " scheme_id=excluded.scheme_id, content_hash=excluded.content_hash,"
            " context_hash=excluded.context_hash, rows_json=excluded.rows_json,"
            " created_at=datetime('now','localtime')", payload)
        await db.commit()
    except Exception as e:
        logger.warning("一致性扫描缓存写入失败（不影响本次结果）: %s", e)


def _norm_topic(t: str) -> str:
    return re.sub(r"[\s·•・,，。.:：;；()（）]+", "", (t or "")).lower()


# 涉及结构/安全的参数主题关键词：不一致至少按 high 处理（与 §5.2 优先级一致）
_SAFETY_TOPIC_HINTS = (
    "强度", "型号", "基坑", "深度", "高度", "脚手架", "支撑",
    "配筋", "截面", "桩", "轴线", "标高", "荷载", "坡度",
)


def _better_topic(a: str, b: str) -> str:
    """合并主题时取更长、更具体的表述（通常来自 AI 判断）。"""
    a, b = a or "", b or ""
    return b if len(b) > len(a) else a


def _conflict_id_prefix(scheme_id: str) -> str:
    """冲突 ID 的**方案作用域前缀**（P0 跨方案碰撞修复，2026-10-04）。

    ✅ BUG 修复（P0）：冲突 ID 此前是全局流水号 ``C{idx:03d}``，每次扫描都从
    C001 重新编号，而 ``consistency_conflicts.id`` 是**全局主键**（不含 scheme_id）。
    后果（实测可推导、与 :func:`persist_conflicts` 的 upsert 语义共同放大）：

      ① 方案 A 扫描写入 C001（scheme_id=A）；方案 B 扫描同样生成 C001 →
         ``ON CONFLICT(id) DO UPDATE`` 命中 A 的行，把 **B 的 scan_id / severity /
         occurrences** 写进 **A 的行**，而旧实现 DO UPDATE **不更新 scheme_id**；
      ② 于是 ``GET /consistency/conflicts?scheme_id=B`` 查不到任何行（返回
         exists=false，用户看到「尚未扫描」）；
      ③ 而 ``GET .../conflicts?scheme_id=A`` 取「最新 scan_id」时取到的是 **B 的
         scan_id**，把 B 方案正文里的冲突原文与章节 ID **展示在 A 方案下** ——
         跨方案数据泄漏 + 冲突清单错乱，且无任何报错。

    修法：ID 内嵌方案作用域（短哈希，确定性、可读、长度可控），不同方案的流水号
    从此不可能互撞。``scheme_id`` 为空时返回空前缀 → **ID 格式与历史完全一致**，
    既有的纯函数单测（不传 scheme_id）与任何不感知方案作用域的调用方零影响。

    ⚠️ 历史落库行（无前缀的 C001…）不做迁移：``get_conflicts`` 按 **最新
    scan_id** 取数（见 consistency_repair.py:124），新扫描必然带新 scan_id，
    旧行自然被隔离，不会出现在结果里；真正需要的是「不再新增碰撞」。
    """
    if not scheme_id:
        return ""
    return f"{hashlib.md5(str(scheme_id).encode('utf-8')).hexdigest()[:6]}-"


def merge_conflicts(ai_rows: list[dict], prescan_rows: list[dict],
                    scheme_id: str = "") -> list[dict]:
    """合并去重：

    1. 同规范化主题归并，occurrences 按「章节 + 取值」去重合并；
    2. 二次去重：若两处冲突「涉及章节 + 取值集合」完全一致，视为同一处不一致
       （消除 AI 命名为「项目总工期」、预扫描命名为「工期」造成的重复）；
    3. 结构/安全类参数（强度/型号/基坑深度…）至少按 high 定级。

    ``scheme_id`` 用于给冲突 ID 加方案作用域前缀（见 :func:`_conflict_id_prefix`），
    **默认空串 = 不加前缀**（向后兼容，ID 形态与改造前逐字一致）。
    """
    _cid_prefix = _conflict_id_prefix(scheme_id)
    merged: dict[str, dict] = {}
    order: list[str] = []

    def _eat(row: dict):
        key = _norm_topic(row["topic"])
        if not key:
            return
        if key not in merged:
            merged[key] = {
                "conflict_type": row.get("conflict_type", "numeric"),
                "topic": row["topic"],
                "occurrences": [],
                "prescan": False,
            }
            order.append(key)
        item = merged[key]
        seen = {(o["section_id"], o.get("value")) for o in item["occurrences"]}
        for occ in row.get("section_occurrences", []):
            sig = (occ.get("section_id"), occ.get("value"))
            if sig in seen:
                continue
            seen.add(sig)
            item["occurrences"].append(occ)
        if row.get("source") == "program_prescan":
            item["prescan"] = True

    # AI 结果优先，预扫描补充，避免预扫描的粗糙类型覆盖 AI 判断
    for row in ai_rows:
        _eat(row)
    for row in prescan_rows:
        _eat(row)

    # 二次去重：章节 + 取值集合完全一致 → 同一处不一致
    sig_map: dict[frozenset, dict] = {}
    deduped: list[dict] = []
    for key in order:
        item = merged[key]
        sig = frozenset((o.get("section_id"), o.get("value")) for o in item["occurrences"])
        if not sig:
            continue
        if sig in sig_map:
            prev = sig_map[sig]
            prev["topic"] = _better_topic(prev["topic"], item["topic"])
            # 保留更具体的冲突类型（非 numeric 优先）
            if item["conflict_type"] != "numeric":
                prev["conflict_type"] = item["conflict_type"]
            seen = {(o["section_id"], o.get("value")) for o in prev["occurrences"]}
            for o in item["occurrences"]:
                s = (o["section_id"], o.get("value"))
                if s not in seen:
                    seen.add(s)
                    prev["occurrences"].append(o)
        else:
            sig_map[sig] = item
            deduped.append(item)

    conflicts = []
    for idx, item in enumerate(deduped, start=1):
        occs = item["occurrences"]
        if not occs:
            continue
        cid = f"{_cid_prefix}C{idx:03d}"
        for o in occs:
            o["conflict_id"] = cid
        values = sorted({o.get("value") for o in occs if o.get("value")})
        severity = _SEVERITY_BY_TYPE.get(item["conflict_type"], "medium")
        if any(h in (item["topic"] or "") for h in _SAFETY_TOPIC_HINTS) and severity != "high":
            severity = "high"
        conflicts.append({
            "id": cid,
            "conflict_type": item["conflict_type"],
            "severity": severity,
            "topic": item["topic"],
            "occurrences": occs,
            "current_values": values,
            "status": "pending",
        })
    return conflicts


async def persist_conflicts(db, scheme_id: str, scan_id: str,
                            conflicts: list[dict]) -> None:
    now = datetime.now().isoformat()
    # ✅ BUG 修复（2026-09-22）：冲突 id 每次扫描都从 C001 重新编号，同一方案
    #    第二次扫描（重新生成正文 → 收尾一致性扫描必再跑一次）插入 C001 时
    #    直接 UNIQUE 冲突 → 整个一致性阶段崩掉（被上层 except 吞掉，表现为
    #    「全文一致性扫描静默失效」）。现改为 upsert：
    #      · 刷新 scan_id / occurrences / severity（以最新一次扫描为准）；
    #      · **保留** status 与仲裁结果（已确认/已修复/已驳回的结论不被重置）。
    #     ✅ P0 补充（2026-10-04）：DO UPDATE 补 `scheme_id=excluded.scheme_id`。
    #     这是**兜底层** —— 即便上游因故仍生成了全局流水号（历史数据 / 自定义
    #     调用方不传 scheme_id），命中他人行时也把它收回本方案，而不是把本方案的
    #     scan_id 与冲突原文写进别人的行（跨方案数据泄漏的放大环节）。
    for c in conflicts:
        await db.execute(
            "INSERT INTO consistency_conflicts "
            "(id, scheme_id, scan_id, conflict_type, severity, topic, occurrences,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " scheme_id=excluded.scheme_id, scan_id=excluded.scan_id,"
            " severity=excluded.severity,"
            " occurrences=excluded.occurrences, created_at=excluded.created_at",
            (c["id"], scheme_id, scan_id, c["conflict_type"], c["severity"],
             c["topic"], json.dumps(c["occurrences"], ensure_ascii=False),
             "pending", now))
    await db.commit()


async def run_scan(db, *, scheme_id: str, project_id: str, scheme_name: str,
                   scheme_type: str,
                   include_global_facts: bool = True,
                   include_project_docs: bool = True,
                   include_design_docs: bool = True,
                   use_cache: bool = True,
                   progress_cb=None) -> dict:
    """执行一次完整扫描，返回 {scan_id, total, conflicts, contexts, cached}。

    progress_cb(done, total, message)：可选的进度回调（SSE/任务注册用）。

    ✅ P0-1（2026-09-22）：扫描由「逐章串行一次 AI」改为
    「增量缓存命中 → 多章合并批扫（并发） → 批失败按章回退」：
      · cached：本次命中缓存、未调用 AI 的章节数（改一章只重扫一章）；
      · use_cache=False 可强制全量重扫（排查缓存问题用）。
    """
    sections = await load_leaf_sections(db, scheme_id)
    if not sections:
        return {"scan_id": "", "total": 0, "conflicts": [], "sections": 0, "cached": 0}

    facts = await build_global_facts_text(db, scheme_id) if include_global_facts else ""
    project_docs = (await build_project_docs_text(db, project_id, limit=3000)
                    if include_project_docs else "")
    design_docs = (await build_project_docs_text(
        db, project_id, doc_type="设计文件", limit=3000)
        if include_design_docs else "")
    standards = build_standards_text(scheme_name, scheme_type)

    # 程序预扫描（零成本强信号）
    prescan_rows = program_prescan(sections)
    logger.info("一致性预扫描：%d 个候选主题", len(prescan_rows))

    # AI 分片扫描：① 增量缓存 ② 多章合并批扫（并发） ③ 批失败按章回退
    total = len(sections)
    by_id = {s["id"]: s for s in sections}
    ctx_hash = context_fingerprint(facts, project_docs, design_docs, standards)
    if use_cache:
        cached_rows, pending = await _load_cached_rows(db, sections, ctx_hash)
    else:
        cached_rows, pending = {}, list(sections)
    ai_rows: list[dict] = []
    for sid, rows in cached_rows.items():
        ai_rows.extend(rows or [])
    # ✅ 2026-09-22（调用次数优化 O6）：批大小按 provider 实时成功率分级
    #    （ai_batch_by_success_rate=False 时与配置值完全一致，零行为变化）。
    _scan_batch_size = await effective_batch_size(CONSISTENCY_SCAN_BATCH_SIZE)
    logger.info("一致性扫描：%d 章（缓存命中 %d，本次需扫 %d，批次 %d）",
                total, len(cached_rows), len(pending),
                len(chunk_sections_for_scan(pending, batch_size=_scan_batch_size)))

    scanned_rows: dict[str, list] = {}
    batches = chunk_sections_for_scan(pending, batch_size=_scan_batch_size)
    if batches:
        sem = asyncio.Semaphore(CONSISTENCY_SCAN_CONCURRENCY)
        done = 0

        async def _scan_batch(batch: list[dict]) -> dict[str, list]:
            nonlocal done
            async with sem:
                out: dict[str, list] = {}
                grouped = None
                if len(batch) > 1:
                    try:
                        grouped = await ai_scan_batch(
                            sections=batch, facts=facts, project_docs=project_docs,
                            design_docs=design_docs, standards=standards)
                    except Exception as e:
                        # ai_scan_batch 内部已兜底，这里是双保险：批路径任何异常
                        # 都必须降级为逐章扫描，不能让整批章节的扫描结果丢失。
                        logger.warning("多章批扫描异常（按章回退）: %s", e)
                        grouped = None
                if grouped is not None:
                    for sec in batch:
                        out[sec["id"]] = normalize_scan_rows(
                            grouped.get(sec["id"], []), sec)
                else:
                    # 单章（批大小=1）或批不可用 → 逐章调用（旧路径，质量兜底）
                    for sec in batch:
                        try:
                            out[sec["id"]] = await ai_scan_section(
                                section=sec, facts=facts, project_docs=project_docs,
                                design_docs=design_docs, standards=standards)
                        except Exception as e:  # ai_scan_section 自身已兜底，双保险
                            logger.warning("章节 %s 扫描失败（跳过）: %s",
                                           sec.get("title"), e)
                            out[sec["id"]] = []
                done += len(batch)
                if progress_cb:
                    await progress_cb(
                        len(cached_rows) + done, total,
                        f"正在扫描章节 {len(cached_rows) + done}/{total}："
                        f"{batch[-1].get('title', '')}")
                return out

        for res in await asyncio.gather(*[_scan_batch(b) for b in batches]):
            scanned_rows.update(res)
        for sid, rows in scanned_rows.items():
            ai_rows.extend(rows or [])
        await _save_cached_rows(db, scheme_id, scanned_rows, ctx_hash, by_id)
    elif progress_cb:
        await progress_cb(total, total, f"全部 {total} 章命中缓存，无需重复扫描")

    # ✅ P0（2026-10-04）：传 scheme_id 让冲突 ID 带方案作用域前缀，杜绝跨方案
    #    主键碰撞（详见 _conflict_id_prefix 的 docstring）。
    conflicts = merge_conflicts(ai_rows, prescan_rows, scheme_id=scheme_id)
    scan_id = new_scan_id()
    await persist_conflicts(db, scheme_id, scan_id, conflicts)
    logger.info("一致性扫描完成：%d 章（缓存 %d）→ %d 个冲突",
                total, len(cached_rows), len(conflicts))
    return {
        "scan_id": scan_id,
        "total": len(conflicts),
        "conflicts": conflicts,
        "sections": total,
        "cached": len(cached_rows),
        "contexts": {
            "global_facts": facts,
            "project_docs": project_docs,
            "design_docs": design_docs,
            "standards": standards,
        },
    }
