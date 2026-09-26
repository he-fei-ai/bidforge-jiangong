"""阶段3 内容分块：按标题层级 / 页 / 表格 / 语义四种策略切分，全部携带 source_ref

对应规范 §3.2 分块策略：

| 分块方式   | 适用       | 块大小  | 重叠  |
| ---------- | ---------- | ------- | ----- |
| 按标题层级 | 结构化文档 | 按章节  | 无    |
| 按页       | PDF 扫描件 | 1 页    | 1 段  |
| 按表格     | 工程量清单 | 1 表    | 无    |
| 按语义     | 长段落     | 500-1000字 | 100字 |

每个 chunk 均带：chunk_id / chunk_type / title / level / page_num /
source_ref（`{doc_id}#page:N#section:标题`）/ 前后链 / 关联 tables/images。
AI 提取结果据此可逐字回溯到原文页码与段落。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# 语义分块（长段落）
SEMANTIC_CHUNK_SIZE = 800      # 500-1000 字区间的中位取值
SEMANTIC_OVERLAP = 100         # 段间重叠 100 字（规范 §3.2）

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
_PAGE_MARK_RE = re.compile(r"<!--\s*page\s*:?\s*(\d+)\s*-->", re.I)
_TABLE_ANCHOR = "【表格】"


def chunk_hash(text: str) -> str:
    """分块内容指纹（增量更新：文件变更后按块级 diff 只重解析变更块）。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _new_chunk(doc_id: str, idx: int, **fields: Any) -> dict:
    c: dict[str, Any] = {
        "chunk_id": f"{doc_id}_c{idx + 1:04d}",
        "doc_id": doc_id,
        "chunk_type": "section",
        "title": "",
        "level": 1,
        "page_num": 1,
        "text": "",
        "source_ref": f"{doc_id}#page:1",
        "parent_chunk_id": None,
        "child_chunk_ids": [],
        "prev_chunk_id": None,
        "next_chunk_id": None,
        "tables": [],
        "images": [],
        "formulas": [],
        "text_offset": 0,
        "text_length": 0,
        "hash": "",
    }
    c.update(fields)
    c["hash"] = chunk_hash(c["text"])
    return c


def _link(chunks: list[dict]) -> list[dict]:
    """串前后链并回填偏移（供语义层 embedding offset 使用）。"""
    offset = 0
    for i, c in enumerate(chunks):
        c["prev_chunk_id"] = chunks[i - 1]["chunk_id"] if i > 0 else None
        c["next_chunk_id"] = chunks[i + 1]["chunk_id"] if i + 1 < len(chunks) else None
        c["text_offset"] = offset
        c["text_length"] = len(c["text"])
        offset += c["text_length"] + 2
    return chunks


def _split_semantic(text: str) -> list[str]:
    """长段落按空行聚合到 ~800 字；单段仍超长则硬切（尾段回退 100 字保持连续）。"""
    out: list[str] = []
    cur = ""

    def _emit_hard(s: str) -> None:
        nonlocal cur
        while len(s) > SEMANTIC_CHUNK_SIZE:
            out.append(s[:SEMANTIC_CHUNK_SIZE])
            s = s[SEMANTIC_CHUNK_SIZE - SEMANTIC_OVERLAP:]
        cur = s

    for p in [x for x in re.split(r"\n\s*\n", text) if x.strip()]:
        if len(p) > SEMANTIC_CHUNK_SIZE:
            if cur:
                out.append(cur)
                cur = ""
            _emit_hard(p)
            continue
        if cur and len(cur) + len(p) + 2 > SEMANTIC_CHUNK_SIZE:
            out.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        out.append(cur)
    return out or ([text] if text.strip() else [])


def chunk_document(markdown: str, *, doc_id: str,
                   structured: dict | None = None) -> list[dict]:
    """对解析层 Markdown 分块。structured 为 md_structured.parse_markdown_structured
    的输出（用于把 tables/images 关联挂到对应块）。
    """
    text = markdown or ""
    if not text.strip():
        return []
    structured = structured or {}
    # 表格/图片按页索引，挂到覆盖该页的块上
    tables_by_page: dict[int, list[str]] = {}
    for t in structured.get("tables", []):
        tables_by_page.setdefault(t.get("page_num", 1), []).append(t["table_id"])
    images_by_page: dict[int, list[str]] = {}
    for im in structured.get("images", []):
        images_by_page.setdefault(im.get("page_num", 1), []).append(im["image_id"])

    lines = text.split("\n")
    chunks: list[dict] = []
    cur_heading = ""
    cur_level = 1
    cur_page = 1
    buf: list[str] = []
    buf_page_start = 1

    def _flush() -> None:
        nonlocal buf, cur_heading
        body = "\n".join(buf).strip()
        if not body:
            buf = []
            return
        base = {
            "doc_id": doc_id, "title": cur_heading, "level": cur_level,
            "page_num": buf_page_start if buf_page_start else cur_page,
        }
        if len(body) > SEMANTIC_CHUNK_SIZE * 2:
            for piece in _split_semantic(body):
                chunks.append(_new_chunk(
                    doc_id, len(chunks), chunk_type="semantic",
                    text=piece,
                    source_ref=(f"{doc_id}#page:{base['page_num']}"
                                f"#section:{cur_heading or 'untitled'}"),
                    **{k: v for k, v in base.items() if k != "doc_id"}))
        else:
            chunks.append(_new_chunk(
                doc_id, len(chunks), chunk_type="section", text=body,
                source_ref=(f"{doc_id}#page:{base['page_num']}"
                            f"#section:{cur_heading or 'untitled'}"),
                **{k: v for k, v in base.items() if k != "doc_id"}))
        buf = []

    for ln in lines:
        m_page = _PAGE_MARK_RE.search(ln.strip())
        if m_page and ln.strip().startswith("<!--"):
            # 页边界：先收尾当前块，保证块不跨页（扫描件按页分块）
            _flush()
            cur_page = int(m_page.group(1))
            buf_page_start = cur_page
            continue
        m_head = _HEADING_RE.match(ln)
        if m_head:
            _flush()
            cur_level = len(m_head.group(1))
            cur_heading = m_head.group(2).strip()[:80]
            if not buf_page_start:
                buf_page_start = cur_page
        if ln.strip() == _TABLE_ANCHOR:
            _flush()
            # 表格独立成块（chunk_type=table），块文本在下方表格行继续累积
            buf_page_start = buf_page_start or cur_page
        if not buf:
            buf_page_start = cur_page
        buf.append(ln)
    _flush()

    # 表格块单独成块：规范 §3.2「按表格分块（1 表，无重叠）」
    table_chunks: list[dict] = []
    for t in structured.get("tables", []):
        hdr = "| " + " | ".join(t.get("headers", [])) + " |" if t.get("headers") else ""
        body = "\n".join("| " + " | ".join(r) + " |" for r in t.get("rows", []))
        tbl_text = (_TABLE_ANCHOR + "\n" + (hdr + "\n" if hdr else "")
                    + body).strip()
        table_chunks.append(_new_chunk(
            doc_id, len(chunks) + len(table_chunks), chunk_type="table",
            title=t.get("title", ""), page_num=t.get("page_num", 1),
            text=tbl_text, source_ref=t.get("source_ref", ""),
            tables=[t["table_id"]]))

    all_chunks = _link(chunks + table_chunks)

    # 把表格/图片 id 关联挂到覆盖对应页的 section 块（AI 提取时可一并注入）
    for c in all_chunks:
        if c["chunk_type"] in ("section", "semantic"):
            pg = c.get("page_num", 1)
            c["tables"] = list(tables_by_page.get(pg, []))
            c["images"] = list(images_by_page.get(pg, []))
    return all_chunks


def chunk_row_of(c: dict, created_at: str = "") -> tuple:
    """chunk dict → doc_chunks 表插入行（列序与 schema_sql 一致）。"""
    import json
    return (
        c["chunk_id"], c["doc_id"], c["chunk_type"], c.get("title", ""),
        c.get("level", 1), c.get("page_num", 1), c.get("text", ""),
        c.get("source_ref", ""), c.get("parent_chunk_id") or "",
        c.get("prev_chunk_id") or "", c.get("next_chunk_id") or "",
        json.dumps(c.get("tables", []), ensure_ascii=False),
        json.dumps(c.get("images", []), ensure_ascii=False),
        c.get("hash", ""),
        json.dumps({
            "formulas": c.get("formulas", []),
            "text_offset": c.get("text_offset", 0),
            "text_length": c.get("text_length", 0),
        }, ensure_ascii=False),
        created_at,
    )
