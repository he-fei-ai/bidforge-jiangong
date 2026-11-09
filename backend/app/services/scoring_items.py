# ✅ R179 M1（2026-11-05）：评分项 → 目录映射校验。
#
# 职责边界：
#   1) 从投标/招标文件的「评标办法 / 评分标准」候选页块抽取评分项（最多 2 批 AI 调用）；
#   2) 与施工组织设计目录做四级匹配（精确 / 包含 / 关键词 / 评分标准回退）；
#   3) 输出 mapped / missing / extra / unmatched 与覆盖率。
#
# 核心规则（不得放宽）：
#   - 页码取 doc_chunks.page_num / source_ref，取不到留空，**不编造**；
#   - 解析不到评分项 = undetermined，coverage_rate = null，items 里不得出现 missing；
#   - 高档评分项（score_band=high）只接受叶子节点；
#   - extra 只查叶子，并排除「祖先已映射」与结构白名单；
#   - 映射关系不落库，按需确定性计算（不引入假缓存）。
#
# 归一化唯一出口 = norm_title（NFKC + 序号前缀剥离 + 标点空白清洗），
# 禁止在其他模块复制第二份实现。
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata

logger = logging.getLogger(__name__)

# 候选页块关键词（命中 ≥2 个才视为「评标办法 / 评分标准」页）
_SECTION_KEYWORDS = (
    "评标办法",
    "评分标准",
    "评分办法",
    "评审因素",
    "评分项",
    "评标细则",
    "综合评分",
    "技术评分",
    "商务评分",
    "分值",
    "打分",
    "得分",
    "扣分",
    "加分",
    "满分",
)

_MAX_DOC_CHARS = 30000
_MAX_BATCHES = 2
_PREFIX_RE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百千0-9]+\s*[章节篇]"
    r"|[0-9]+(?:\s*[.．]\s*[0-9]+)*\s*[、.．)）]?"
    r"|[一二三四五六七八九十]+\s*、)\s*"
)
_STRIP_RE = re.compile(r"[#>*`|_~\-—\[\]()（）【】:：,，。；;、'\"“”‘’!！?？\s]+")
# 中文分词：按标点/空白/常见连接词切段，中文段内展开 2-gram，
# 使「关键词 Jaccard」在中文标题上具备实际判别力（纯标点切分几乎恒为 0）。
_TOKEN_SPLIT = re.compile(r"[、，,。；;：:（）()【】\[\]《》<>\"'“”‘’\s/|｜与及和或的之]+")
_STOPWORDS = {
    "施工",
    "方案",
    "措施",
    "工程",
    "技术",
    "管理",
    "要求",
    "方法",
    "内容",
    "本章",
    "以下",
    "工作",
    "及",
    "与",
    "和",
    "的",
    "为",
    "对",
    "在",
}


# ---------------------------------------------------------------------------
# 归一化（唯一出口）
# ---------------------------------------------------------------------------
def norm_title(text: str) -> str:
    """标题归一：NFKC（全角→半角）→ casefold → 剥离序号前缀 → 去标点空白。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.casefold()
    s = _PREFIX_RE.sub("", s)
    return _STRIP_RE.sub("", s)


def tokens(text: str) -> set[str]:
    """关键词集合：ASCII 按词，中文按 2-gram 展开。

    中文标题没有空格，按标点整段算一个 token 会让 Jaccard 几乎恒为 0；
    统一改成 2-gram，才能与关键词阈值（0.45 / 0.55 / 0.65）同一量纲。
    """
    out: set[str] = set()
    for seg in _TOKEN_SPLIT.split(str(text or "")):
        seg = seg.strip()
        if not seg or len(seg) < 2:
            continue
        if seg.isascii():
            out.add(seg)
            continue
        for i in range(len(seg) - 1):
            out.add(seg[i : i + 2])
    return out


def keywords_of(text: str) -> set[str]:
    """关键词集合：剔除高频无区分度词，避免 Jaccard 被「施工/方案」灌水。"""
    return {t for t in tokens(text) if t not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# 分值 / 档位
# ---------------------------------------------------------------------------
def parse_score_value(score_raw: str, item_name: str = "", criteria: str = "") -> float | None:
    """分值解析：原文没有分值 → None（不按 0 处理，不编造）。"""
    for source in (score_raw, item_name, criteria):
        if not source:
            continue
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*分", source)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
        if source is score_raw:
            m2 = re.search(r"([0-9]+(?:\.[0-9]+)?)", source)
            if m2:
                try:
                    return float(m2.group(1))
                except ValueError:
                    continue
    return None


def compute_score_bands(items: list[dict]) -> dict[str, str]:
    """按分值占比计算 score_band：high ≥0.15，low <0.05，其余 mid。

    任一分值缺失 → 该评分项 mid；全部缺失 → 全部 mid。
    """
    total = sum(float(i["score"]) for i in items if i.get("score") is not None)
    bands: dict[str, str] = {}
    for it in items:
        score = it.get("score")
        if score is None or total <= 0:
            bands[str(it.get("id") or "")] = "mid"
            continue
        ratio = float(score) / total
        if ratio >= 0.15:
            bands[str(it.get("id") or "")] = "high"
        elif ratio < 0.05:
            bands[str(it.get("id") or "")] = "low"
        else:
            bands[str(it.get("id") or "")] = "mid"
    return bands


# ---------------------------------------------------------------------------
# 源文档 / 候选页块选择
# ---------------------------------------------------------------------------
def keyword_hits(text: str) -> int:
    return sum(1 for kw in _SECTION_KEYWORDS if kw in (text or ""))


def select_page_blocks(pages: list[dict], *, max_chars: int = _MAX_DOC_CHARS) -> tuple[str, list[int], int]:
    """单文档候选页选择：命中 ≥2 关键词的页 + 相邻 ±1 页上下文。

    返回 (拼接文本, 命中页码列表, 命中页块数)。取不到页码时不编造。
    """
    hits: list[dict] = []
    for p in pages:
        if keyword_hits(str(p.get("text") or "")) >= 2:
            hits.append(p)
    if not hits:
        return "", [], 0

    by_num = {int(p.get("page_num") or 0): p for p in pages}
    picked: dict[int, dict] = {}
    for p in hits:
        n = int(p.get("page_num") or 0)
        for delta in (-1, 0, 1):
            cand = by_num.get(n + delta)
            if cand is not None:
                picked[int(cand.get("page_num") or 0)] = cand

    ordered = [picked[k] for k in sorted(picked)]
    parts: list[str] = []
    used_pages: list[int] = []
    total = 0
    for p in ordered:
        n = int(p.get("page_num") or 0)
        chunk = f"\n【第{n}页】\n{str(p.get('text') or '')}"
        if total + len(chunk) > max_chars:
            break
        parts.append(chunk)
        used_pages.append(n)
        total += len(chunk)
    return "".join(parts).strip(), used_pages, len(used_pages)


async def select_scoring_sources(db, project_id: str) -> list[dict]:
    """扫描项目全部文档，确定性筛出候选页块（v1 不接受 doc_ids 限定范围）。

    ✅ R191 修复（chunk_type='page' 死契约）：doc_chunker 从不产 'page' 块
    （只产 section/semantic/table 三类，schema 注释的四类声明从未兑现），
    旧查询恒取 0 行 → 本函数恒走「无页块」兜底分支——评分项提取的页级
    定位与 M1 页码溯源从未真正生效（静默降级）。现改读实际存在的
    section/semantic 块并按 page_num 聚合成页块（同页多块正文合并，
    source_ref 取该页首块），页级选择能力自此可达；页码口径不变
    （doc_chunks.page_num / source_ref，取不到留空，不编造）。

    ✅ R191（R13 判空）：两处 db.execute 游标 None 时降级留痕（主游标 →
    返回空列表，调用方走既有 failed run 语义；块游标 → 跳过该文档），
    不再裸 fetchall 抛 AttributeError。
    """
    cur = await db.execute(
        "SELECT id, file_name, parsed_markdown FROM project_documents WHERE project_id=?",
        (project_id,),
    )
    if cur is None:
        logger.warning("评分项提取：查询项目文档失败（db.execute 返回 None，R13），project=%s", project_id)
        return []
    docs = await cur.fetchall()

    sources: list[dict] = []
    for doc in docs:
        doc_id = str(_row(doc, "id", 0) or "")
        name = str(_row(doc, "file_name", 1) or "")
        parsed = str(_row(doc, "parsed_markdown", 2) or "")
        cur2 = await db.execute(
            "SELECT page_num, text, source_ref FROM doc_chunks "
            "WHERE doc_id=? AND chunk_type IN ('section','semantic') "
            "ORDER BY page_num, rowid",
            (doc_id,),
        )
        if cur2 is None:
            logger.warning("评分项提取：查询文档分块失败（db.execute 返回 None，R13），doc=%s", doc_id)
            continue
        rows = await cur2.fetchall()
        # 按 page_num 聚合成页块：同页多个 section/semantic 块正文拼接
        _by_page: dict = {}
        for r in rows:
            pn = _row(r, "page_num", 0)
            key = pn if isinstance(pn, int) and pn > 0 else 1
            entry = _by_page.get(key)
            if entry is None:
                _by_page[key] = {
                    "page_num": key,
                    "text": str(_row(r, "text", 1) or ""),
                    "source_ref": str(_row(r, "source_ref", 2) or ""),
                }
            else:
                entry["text"] = (entry["text"] + "\n" + str(_row(r, "text", 1) or "")).strip("\n")
        pages = [_by_page[k] for k in sorted(_by_page)]
        if pages:
            text, used_pages, hits = select_page_blocks(pages, max_chars=_MAX_DOC_CHARS)
            if not text:
                continue
        else:
            # 无页块：仅当解析全文命中 ≥2 关键词才兜底，页码留空（不编造）
            hits = keyword_hits(parsed)
            if hits < 2 or not parsed.strip():
                continue
            text = parsed[:_MAX_DOC_CHARS]
            used_pages = []
        sources.append(
            {
                "doc_id": doc_id,
                "name": name,
                "text": text,
                "page_nums": used_pages,
                "pages": pages,
                "keyword_hits": hits,
            }
        )
    return sources


def _row(row, key: str, idx: int):
    """兼容 dict / sqlite3.Row / tuple 三种 row 形态。"""
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:  # noqa: BLE001 - 兼容行对象按键取值失败，随即回退下标访问
            pass
    return row[idx]


def plan_batches(
    sources: list[dict], *, max_chars: int = 60000, max_docs: int = 4
) -> tuple[list[list[dict]], list[dict]]:
    """把候选源切成 ≤2 批，单批 ≤max_chars；超预算文档记为 failed source（不静默丢）。"""
    ordered = sorted(sources, key=lambda s: (-int(s.get("keyword_hits") or 0), len(s.get("text") or "")))
    batches: list[list[dict]] = []
    overflow: list[dict] = []
    cur: list[dict] = []
    cur_chars = 0
    used_docs = 0
    for s in ordered:
        if used_docs >= max_docs:
            overflow.append(
                {
                    "doc_id": s["doc_id"],
                    "file_name": s.get("name") or "",
                    "reason": f"超出单次抽取文档数上限（{max_docs}）",
                }
            )
            continue
        size = len(s.get("text") or "")
        if cur and (cur_chars + size > max_chars or len(cur) >= max_docs):
            batches.append(cur)
            cur, cur_chars = [], 0
        if not cur and len(batches) >= _MAX_BATCHES:
            overflow.append(
                {
                    "doc_id": s["doc_id"],
                    "file_name": s.get("name") or "",
                    "reason": f"超出单次解析预算（最多 {_MAX_BATCHES} 批）",
                }
            )
            continue
        cur.append(s)
        cur_chars += size
        used_docs += 1
    if cur and len(batches) < _MAX_BATCHES:
        batches.append(cur)
    elif cur:
        overflow.extend(
            {
                "doc_id": s["doc_id"],
                "file_name": s.get("name") or "",
                "reason": f"超出单次解析预算（最多 {_MAX_BATCHES} 批）",
            }
            for s in cur
        )
    return batches, overflow


def _validate_scoring_obj(obj) -> list[str]:
    if not isinstance(obj, dict):
        return ['顶层必须是对象：{"items": [...]}']
    if not isinstance(obj.get("items"), list):
        return ["items 必须是数组"]
    return []


async def extract_batch(batch: list[dict], *, timeout: int = 120) -> list[dict]:
    """单批 AI 抽取（失败抛异常，由上层按批降级）。

    返回该批全部评分项；每项带 ``_batch_doc_ids``（本批来源文档，供证据定位）。
    """
    from app.services.ai.json_response import collect_json_response
    from app.services.ai.prompts._registry import render

    text = "\n\n".join(
        f"【来源文档】{s.get('name') or s.get('doc_id')}\n【正文】\n{s.get('text') or ''}" for s in batch
    )
    system = render("scoring_items_extract", scoring_text=text)
    obj, _raw = await collect_json_response(
        [{"role": "system", "content": system}],
        validate_fn=_validate_scoring_obj,
        temperature=0.1,
        json_mode=True,
        timeout=timeout,
        scene="scoring_items_extract",
    )
    doc_ids = [s.get("doc_id") or "" for s in batch]
    names = [s.get("name") or "" for s in batch]
    out: list[dict] = []
    for it in obj.get("items") or []:
        if not isinstance(it, dict):
            continue
        out.append(
            {
                **it,
                "_batch_doc_ids": doc_ids,
                "_source_doc_id": doc_ids[0] if doc_ids else "",
                "_source_name": names[0] if names else "",
            }
        )
    return out


def _sha1_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def attach_evidence(item_name: str, quote: str, pages: list[dict], source_doc_id: str) -> dict:
    """用页块反查证据：返回 {page_num, page_ref, source_quote}；匹配不上留空，不编造。"""
    needles = [n for n in (str(quote or "").strip(), str(item_name or "").strip()) if n and len(n) >= 3]
    for p in pages:
        text = str(p.get("text") or "")
        for n in needles:
            if n in text:
                num = p.get("page_num")
                ref = str(p.get("source_ref") or "").strip()
                page_num = int(num) if num not in (None, "") else None
                if ref:
                    page_ref = ref
                elif page_num is not None:
                    page_ref = f"{source_doc_id}#page:{page_num}"
                else:
                    page_ref = ""
                return {"page_num": page_num, "page_ref": page_ref, "source_quote": (str(quote or n))[:120]}
    return {"page_num": None, "page_ref": "", "source_quote": str(quote or "")[:120]}


def normalize_and_validate(
    project_id: str, raw_items: list[dict], page_index: dict[str, list[dict]] | None = None
) -> list[dict]:
    """必填校验 + 分值正则 + 去重 + 资格性条款剔除 + 证据定位。"""
    page_index = page_index or {}
    out: list[dict] = []
    seen: dict[str, int] = {}
    for raw in raw_items:
        name = str(raw.get("item_name") or raw.get("name") or "").strip()
        if not name:
            continue  # 不编造名称
        if raw.get("is_scoring_item") is False:
            continue
        doc_id = str(raw.get("_source_doc_id") or "")
        batch_docs = [str(d) for d in (raw.get("_batch_doc_ids") or [doc_id]) if d]
        pages: list[dict] = []
        for d in batch_docs:
            pages.extend(page_index.get(d, []) or [])

        item_no = str(raw.get("item_no") or "").strip()
        score_raw = str(raw.get("score_raw") or "").strip()
        criteria = str(raw.get("criteria") or "").strip()
        score = parse_score_value(score_raw, name, criteria)
        evidence = attach_evidence(name, str(raw.get("quote") or raw.get("evidence") or ""), pages, doc_id)
        item = {
            "id": _sha1_id(project_id, doc_id, norm_title(item_no), norm_title(name)),
            "project_id": project_id,
            "source_doc_id": doc_id,
            "source_name": str(raw.get("_source_name") or ""),
            "item_no": item_no,
            "item_name": name,
            "score": score,
            "score_raw": score_raw,
            "criteria": criteria,
            "is_primary": 1 if raw.get("is_primary") else 0,
            **evidence,
        }
        key = item["id"]
        if key in seen:
            # 同一评分项重复出现：保留分值更高 / 证据更全的一条
            old = out[seen[key]]
            old_score = old.get("score")
            if (score is not None and (old_score is None or score > old_score)) or (
                len(criteria) > len(old.get("criteria") or "")
            ):
                out[seen[key]] = item
            continue
        seen[key] = len(out)
        out.append(item)
    return out


async def persist_items(db, project_id: str, items: list[dict], *, run_status: str) -> None:
    """按 id UPSERT；仅当本次 run 为 success **且有评分项** 才把未覆盖的旧行标 stale（不删数据）。

    空结果一律不标 stale：模型偶发返回 0 条时，活跃快照必须原样保留，
    否则数据还在但等于不可用（0 条由路由侧判 empty，与「真的没有评分项」区分）。
    """
    for it in items:
        await db.execute(
            "INSERT INTO scheme_scoring_items ("
            "id, project_id, source_doc_id, source_name, item_no, item_name, score, score_raw,"
            "criteria, is_primary, page_ref, page_num, source_quote, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')"
            " ON CONFLICT(id) DO UPDATE SET"
            " source_doc_id=excluded.source_doc_id, source_name=excluded.source_name,"
            " item_no=excluded.item_no, item_name=excluded.item_name, score=excluded.score,"
            " score_raw=excluded.score_raw, criteria=excluded.criteria, is_primary=excluded.is_primary,"
            " page_ref=excluded.page_ref, page_num=excluded.page_num, source_quote=excluded.source_quote,"
            " status='active', updated_at=datetime('now','localtime')",
            (
                it["id"],
                project_id,
                it["source_doc_id"],
                it["source_name"],
                it["item_no"],
                it["item_name"],
                it["score"],
                it["score_raw"],
                it["criteria"],
                it["is_primary"],
                it["page_ref"],
                it["page_num"],
                it["source_quote"],
            ),
        )
    if run_status == "success" and items:
        ids = [it["id"] for it in items]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE scheme_scoring_items SET status='stale'"
            f" WHERE project_id=? AND status='active' AND id NOT IN ({placeholders})",
            (project_id, *ids),
        )


async def insert_run(db, run: dict) -> None:
    await db.execute(
        "INSERT INTO scheme_scoring_extract_runs ("
        "id, project_id, status, docs_total, docs_ok, failed_sources, items_count, error, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))",
        (
            run["id"],
            run["project_id"],
            run["status"],
            run.get("docs_total") or 0,
            run.get("docs_ok") or 0,
            json.dumps(run.get("failed_sources") or [], ensure_ascii=False),
            run.get("items_count") or 0,
            run.get("error") or "",
        ),
    )


async def load_active_items(db, project_id: str) -> list[dict]:
    cur = await db.execute(
        "SELECT id, source_doc_id, source_name, item_no, item_name, score, score_raw, criteria,"
        " is_primary, page_ref, page_num, source_quote"
        " FROM scheme_scoring_items WHERE project_id=? AND status='active'"
        " ORDER BY item_no, item_name",
        (project_id,),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": _row(r, "id", 0),
            "source_doc_id": _row(r, "source_doc_id", 1) or "",
            "source_name": _row(r, "source_name", 2) or "",
            "item_no": _row(r, "item_no", 3) or "",
            "item_name": _row(r, "item_name", 4) or "",
            "score": _row(r, "score", 5),
            "score_raw": _row(r, "score_raw", 6) or "",
            "criteria": _row(r, "criteria", 7) or "",
            "is_primary": _row(r, "is_primary", 8) or 0,
            "page_ref": _row(r, "page_ref", 9) or "",
            "page_num": _row(r, "page_num", 10),
            "source_quote": _row(r, "source_quote", 11) or "",
        }
        for r in rows
    ]


async def load_latest_run(db, project_id: str) -> dict | None:
    cur = await db.execute(
        "SELECT id, status, docs_total, docs_ok, failed_sources, items_count, error, created_at, finished_at"
        " FROM scheme_scoring_extract_runs WHERE project_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (project_id,),
    )
    r = await cur.fetchone()
    if not r:
        return None
    try:
        failed = json.loads(_row(r, "failed_sources", 4) or "[]")
    except Exception:
        failed = []
    return {
        "run_id": _row(r, "id", 0),
        "status": _row(r, "status", 1) or "running",
        "docs_total": _row(r, "docs_total", 2) or 0,
        "docs_ok": _row(r, "docs_ok", 3) or 0,
        "failed_sources": failed,
        "items_count": _row(r, "items_count", 5) or 0,
        "error": _row(r, "error", 6) or "",
        "updated_at": _row(r, "finished_at", 8) or _row(r, "created_at", 7) or "",
    }


# ---------------------------------------------------------------------------
# 目录结构
# ---------------------------------------------------------------------------
def build_outline_tree(rows: list[dict]) -> list[dict]:
    nodes: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        nid = str(r.get("id") or "")
        if not nid:
            continue
        nodes[nid] = {
            "id": nid,
            "parent_id": str(r.get("parent_id") or ""),
            "title": str(r.get("title") or ""),
            "sort_order": int(r.get("sort_order") or 0),
            "children": [],
        }
        order.append(nid)
    roots: list[dict] = []
    for nid in order:
        node = nodes[nid]
        pid = node["parent_id"]
        if pid and pid in nodes:
            nodes[pid]["children"].append(node)
        else:
            roots.append(node)
    return roots


def flatten_outline(outline: list[dict]) -> list[dict]:
    flat: list[dict] = []

    def _walk(nodes: list, level: int, prefix: str) -> None:
        for idx, n in enumerate(nodes or []):
            if not isinstance(n, dict):
                continue
            children = n.get("children") if isinstance(n.get("children"), list) else []
            path = f"{prefix}.{idx + 1}" if prefix else str(idx + 1)
            flat.append(
                {
                    "id": str(n.get("id") or ""),
                    "parent_id": str(n.get("parent_id") or ""),
                    "title": str(n.get("title") or ""),
                    "level": level,
                    "path": path,
                    "sort_order": idx,
                    "has_children": bool(children),
                }
            )
            _walk(children, level + 1, path)

    _walk(outline, 1, "")
    return flat


def ancestors_of(section: dict, by_id: dict[str, dict]) -> set[str]:
    out: set[str] = set()
    cur = section
    guard = 0
    while cur and guard < 20:
        pid = str(cur.get("parent_id") or "")
        if not pid or pid not in by_id:
            break
        out.add(pid)
        cur = by_id[pid]
        guard += 1
    return out


# ---------------------------------------------------------------------------
# 四级匹配
# ---------------------------------------------------------------------------
def thresholds_config(cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    return {
        "exact": float(cfg.get("exact", 0.95)),
        "contains": float(cfg.get("contains", 0.85)),
        "keyword_high": float(cfg.get("keyword_high", 0.65)),
        "keyword_mid": float(cfg.get("keyword_mid", 0.55)),
        "keyword_low": float(cfg.get("keyword_low", 0.45)),
        "criteria_max": 0.5,
    }


def threshold_config_from_settings(settings_obj=None) -> dict:
    if settings_obj is None:
        try:
            from app.config import settings as settings_obj  # type: ignore[assignment]
        except Exception:  # 配置不可用 → 用默认阈值，不阻断校验
            settings_obj = None
    if settings_obj is None:
        return thresholds_config({})
    return thresholds_config(
        {
            "exact": getattr(settings_obj, "scoring_threshold_exact", 0.95),
            "contains": getattr(settings_obj, "scoring_threshold_contains", 0.85),
            "keyword_high": getattr(settings_obj, "scoring_threshold_keyword_high", 0.65),
            "keyword_mid": getattr(settings_obj, "scoring_threshold_keyword_mid", 0.55),
            "keyword_low": getattr(settings_obj, "scoring_threshold_keyword_low", 0.45),
        }
    )


def match_one(item: dict, section: dict, section_text: str, cfg: dict) -> dict | None:
    """单评分项 vs 单章节：每对只取命中的最高一级。"""
    t_item = norm_title(item.get("item_name") or "")
    t_sec = norm_title(section.get("title") or "")
    if not t_item or not t_sec:
        return None
    if t_item == t_sec:
        return {"method": "exact", "confidence": cfg["exact"], "needs_review": False}
    short, long_ = (t_item, t_sec) if len(t_item) <= len(t_sec) else (t_sec, t_item)
    if len(short) >= 4 and short in long_ and len(short) / len(long_) >= 0.60:
        return {"method": "contains", "confidence": cfg["contains"], "needs_review": False}

    band = str(item.get("score_band") or "mid")
    j = jaccard(keywords_of(item.get("item_name") or ""), keywords_of(section.get("title") or ""))
    thr = {"high": cfg["keyword_high"], "mid": cfg["keyword_mid"], "low": cfg["keyword_low"]}.get(
        band, cfg["keyword_mid"]
    )
    if j >= thr:
        return {"method": "keyword", "confidence": min(0.90, 0.50 + 0.40 * j), "needs_review": False}

    # S4 评分标准回退：criteria 与章节正文共享 ≥2 个内容 token 且 Jaccard 达档位阈值
    kw_item = keywords_of(item.get("criteria") or "")
    kw_sec = keywords_of(section_text or section.get("title") or "")
    jc = jaccard(kw_item, kw_sec)
    thr4 = {"high": 0.45, "mid": 0.40, "low": 0.30}.get(band, 0.40)
    if len(kw_item & kw_sec) >= 2 and jc >= thr4:
        return {
            "method": "criteria",
            "confidence": min(cfg["criteria_max"], 0.30 + 0.40 * jc),
            "needs_review": True,
        }
    return None


def _is_generic_title(title: str) -> bool:
    try:
        from app.services.outline_quality import is_generic_title

        return bool(is_generic_title(title))
    except Exception:
        return False


def _suggest_section(item: dict, sections: list[dict]) -> dict:
    """缺失项 → 「新增建议章节」（只给建议，不落库、不生成正文）。"""
    # 父节点建议：优先最相近的二级章节；找不到则挂到最后一个一级章节
    parents = [s for s in sections if s.get("level") == 2]
    best: dict | None = None
    best_score = -1.0
    kw = keywords_of(item.get("item_name") or "")
    for p in parents:
        score = jaccard(kw, keywords_of(p.get("title") or ""))
        if score > best_score:
            best, best_score = p, score
    if best is None:
        level1 = [s for s in sections if s.get("level") == 1]
        best = level1[-1] if level1 else None
    return {
        "title": str(item.get("item_name") or ""),
        "level": min(int((best or {}).get("level") or 1) + 1, 3),
        "parent_id": (best or {}).get("id") or None,
        "parent_title": (best or {}).get("title") or "",
    }


def build_coverage_report(
    items: list[dict],
    sections: list[dict],
    section_texts: dict[str, str] | None = None,
    thresholds: dict | None = None,
    extra_whitelist: list[str] | None = None,
    extraction_status: str = "success",
    failed_sources: list[dict] | None = None,
) -> dict:
    """生成覆盖率报告。

    extraction_status ∈ success | partial | failed | none；
    报告 status ∈ ok | partial | undetermined。
    ``undetermined`` 时 coverage_rate=null、items 里不得出现 missing。
    """
    cfg = thresholds_config(thresholds)
    section_texts = section_texts or {}
    extra_whitelist = extra_whitelist or []

    if not items:
        reason = "extraction_failed" if extraction_status in ("failed", "none") else "empty_items"
        return {
            "status": "undetermined",
            "undetermined_reason": reason,
            "items": [],
            "extra": [],
            "missing": [],
            "unmatched": failed_sources or [],
            "overloaded_sections": [],
            "summary": {
                "total_items": 0,
                "mapped": 0,
                "missing": 0,
                "extra": 0,
                "unmatched": len(failed_sources or []),
                "coverage_rate": None,
                "undetermined_items": 0,
            },
        }

    bands = compute_score_bands(items)
    leaf_ids = {s["id"] for s in sections if not s.get("has_children")}
    by_id = {s["id"]: s for s in sections}

    result_items: list[dict] = []
    item_counts: dict[str, int] = {}

    for item in items:
        band = bands.get(str(item.get("id") or ""), "mid")
        cands: list[tuple[dict, dict]] = []
        for sec in sections:
            m = match_one({**item, "score_band": band}, sec, section_texts.get(sec["id"], ""), cfg)
            if not m:
                continue
            # 高档评分项只接受叶子节点（只命中父节点 → 不接受，按 missing 统计）
            if band == "high" and sec["id"] not in leaf_ids:
                continue
            cands.append((m, sec))

        base = {
            "id": item.get("id"),
            "item_no": item.get("item_no") or "",
            "item_name": item.get("item_name") or "",
            "score": item.get("score"),
            "score_raw": item.get("score_raw") or "",
            "score_band": band,
            "is_primary": bool(item.get("is_primary")),
            "page_ref": item.get("page_ref") or "",
            "page_num": item.get("page_num"),
            "source_doc_id": item.get("source_doc_id") or "",
            "source_quote": item.get("source_quote") or "",
            "criteria": item.get("criteria") or "",
        }
        if not cands:
            base.update(
                {
                    "status": "missing",
                    "method": None,
                    "confidence": None,
                    "section_id": None,
                    "section_title": None,
                    "matched_sections": [],
                    "also_section_ids": [],
                    "needs_review": False,
                    "suggestion": _suggest_section(item, sections),
                    "reason": "未找到达阈值的章节",
                }
            )
            result_items.append(base)
            continue

        # 一评多章节：confidence desc → level desc → sort_order asc
        ranked = sorted(
            cands,
            key=lambda x: (-x[0]["confidence"], -int(x[1].get("level") or 0), int(x[1].get("sort_order") or 0)),
        )
        primary_m, primary_s = ranked[0]
        base.update(
            {
                "status": "mapped",
                "method": primary_m["method"],
                "confidence": round(primary_m["confidence"], 3),
                "section_id": primary_s["id"],
                "section_title": primary_s["title"],
                "matched_sections": [
                    {
                        "section_id": s["id"],
                        "title": s["title"],
                        "level": s["level"],
                        "confidence": round(m["confidence"], 3),
                        "primary": idx == 0,
                    }
                    for idx, (m, s) in enumerate(ranked)
                ],
                "also_section_ids": [s["id"] for _m, s in ranked[1:]],
                "needs_review": bool(primary_m["needs_review"]),
                "suggestion": None,
                "reason": "",
            }
        )
        result_items.append(base)
        item_counts[primary_s["id"]] = item_counts.get(primary_s["id"], 0) + 1

    mapped = [i for i in result_items if i["status"] == "mapped"]
    missing = [i for i in result_items if i["status"] == "missing"]
    coverage_rate = round(len(mapped) / len(result_items), 4) if result_items else None

    # extra：叶子 + 未被命中 + 祖先未被命中 + 非白名单 + 非泛化标题
    mapped_ids: set[str] = set()
    for i in mapped:
        if i.get("section_id"):
            mapped_ids.add(i["section_id"])
        mapped_ids.update(i.get("also_section_ids") or [])
    whitelist = {norm_title(w) for w in extra_whitelist}

    extra: list[dict] = []
    for sec in sections:
        if sec["id"] not in leaf_ids or sec["id"] in mapped_ids:
            continue
        if ancestors_of(sec, by_id) & mapped_ids:
            continue
        if norm_title(sec["title"]) in whitelist:
            continue
        if _is_generic_title(sec["title"]):
            continue
        extra.append(
            {
                "section_id": sec["id"],
                "title": sec["title"],
                "level": sec["level"],
                "path": sec["path"],
                "parent_id": sec.get("parent_id") or "",
                "status": "extra",
                "reason": "无评分项支撑",
            }
        )

    # 多个评分项命中同一章节（仅提示，不影响 mapped）
    leaf_count = len(leaf_ids) or 1
    overload_threshold = max(3, math.ceil(len(result_items) / leaf_count))
    overloaded = [
        {"section_id": sid, "title": by_id[sid]["title"], "matched_item_count": cnt}
        for sid, cnt in item_counts.items()
        if cnt >= overload_threshold and sid in by_id
    ]
    overloaded.sort(key=lambda x: -x["matched_item_count"])

    failed_sources = failed_sources or []
    report_status = "partial" if (extraction_status == "partial" or failed_sources) else "ok"

    return {
        "status": report_status,
        "undetermined_reason": None,
        "items": result_items,
        "extra": extra,
        "missing": [
            {
                "item_id": i["id"],
                "item_no": i["item_no"],
                "name": i["item_name"],
                "score": i["score"],
                "score_raw": i["score_raw"],
                "suggestion": i["suggestion"],
                "page_num": i["page_num"],
                "page_ref": i["page_ref"],
                "reason": i["reason"],
            }
            for i in missing
        ],
        "unmatched": [{"kind": "source", **s} for s in failed_sources],
        "overloaded_sections": overloaded,
        "summary": {
            "total_items": len(result_items),
            "mapped": len(mapped),
            "missing": len(missing),
            "extra": len(extra),
            "unmatched": len(failed_sources),
            "coverage_rate": coverage_rate,
            "undetermined_items": 0,
        },
    }
