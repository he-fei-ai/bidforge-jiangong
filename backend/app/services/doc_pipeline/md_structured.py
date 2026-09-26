"""解析层结构化抽取：从结构化 Markdown 还原 分页文本 / 表格 JSON / 图片清单

对应规范 §2.2 解析层四份产物。输入为 file_parser 的输出（含 `<!-- page:N -->`
页标记、`【表格】` + GFM 管道表、`![alt](src)` / `[IMAGE: ...]` 图片标记），
输出符合规范 schema 的 pages / tables / images 三份 JSON 结构。

可追溯性：每张表格/图片均归属页码，并生成 source_ref
（`{doc_id}#page:N#table:tK` / `{doc_id}#page:N#img:iK`）。
"""
from __future__ import annotations

import re
from typing import Any

# 页标记（与 file_parser._PDF_PAGE_MARK_RE 同口径，独立定义避免私有耦合）
_PAGE_MARK_RE = re.compile(r"<!--\s*page\s*:?\s*(\d+)\s*-->", re.I)
# 表格标题候选：表格块前最近的一行非空普通文本（如 "工程规模表"、"表 2-1 xxx"）
_TABLE_TITLE_RE = re.compile(r"^(?:[【表]\s*(?:表格|表)\s*[\d\-—\.]*[】]?\s*)?(.+)$")
# Markdown 分隔行（| --- | :--: |）
_SEP_ROW_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$")
# 图片标记：![](src) / [IMAGE: file, page:N] / 图N-M 题注行
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_BRACKET_IMG_RE = re.compile(
    r"\[\s*IMAGE\s*[:：]\s*([^\],]+?)\s*(?:[,，]\s*page\s*:?\s*(\d+)\s*)?\]",
    re.I)
_CAPTION_RE = re.compile(r"^[（(]?\s*(?:图|附圖|附图)\s*[\d]+(?:[-—.]\d+)?\s*[）)]?\s*(.*)$")
# 公式标记：块级 $$...$$ 与行内 \( \) / 常见公式特征
_BLOCK_MATH_RE = re.compile(r"\$\$([\s\S]+?)\$\$")


def _split_pipe_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _extract_title(candidate: str) -> str:
    """从表格上方文本行提取标题（过长说明行不作为标题）。"""
    if not candidate:
        return ""
    t = candidate.strip().strip("#").strip()
    m = _TABLE_TITLE_RE.match(t)
    title = (m.group(1) if m else t).strip()
    return title[:60] if 0 < len(title) <= 60 else ""


def parse_markdown_structured(markdown: str, *, doc_id: str = "") -> dict[str, Any]:
    """Markdown → {pages, tables, images, formulas, page_count, meta}。

    - pages：[{page_num, text, tables:[table_id], images:[image_id], formulas:[..]}]
    - tables：[{table_id, page_num, title, headers, rows, data, source_ref}]
    - images：[{image_id, page_num, file_path/alt, caption, ocr_text, source_ref}]
    """
    text = markdown or ""
    lines = text.split("\n")

    # ---- 1. 页切分：按 page 标记把行归页（标记前内容归第 1 页）----
    page_of_line: list[int] = []
    cur_page = 1
    for ln in lines:
        m = _PAGE_MARK_RE.search(ln.strip())
        if m and ln.strip().startswith("<!--"):
            cur_page = int(m.group(1))
            page_of_line.append(cur_page)
            continue
        page_of_line.append(cur_page)

    pages: dict[int, dict] = {}

    def _page_entry(num: int) -> dict:
        return pages.setdefault(num, {
            "page_num": num, "line_idx": [], "tables": [], "images": [],
            "formulas": [],
        })

    for i, ln in enumerate(lines):
        if _PAGE_MARK_RE.search(ln.strip()) and ln.strip().startswith("<!--"):
            _page_entry(page_of_line[i])
            continue
        _page_entry(page_of_line[i])["line_idx"].append(i)

    # ---- 2. 表格抽取（全篇顺序扫描，保证跨页表格也归位起始页）----
    tables: list[dict] = []
    consumed: set[int] = set()   # 已被表格消费的行的全局行号
    last_plain = ""              # 表格前最近的普通文本行（标题候选）
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        is_table_anchor = stripped == "【表格】" or (
            stripped.startswith("|") and i + 1 < len(lines)
            and _SEP_ROW_RE.match(lines[i + 1].strip() or ""))
        if not is_table_anchor:
            if stripped and not stripped.startswith("#"):
                last_plain = stripped
            i += 1
            continue
        start = i
        if stripped == "【表格】":
            i += 1
        header_line_idx = None
        while i < len(lines) and lines[i].strip().startswith("|"):
            if header_line_idx is None:
                header_line_idx = i
            i += 1
        # GFM 合法表：表头行的【下一行】必须是分隔行（| --- | :--: |）。
        # 旧实现误判 lines[i]（此时 i 已推进到整块表格之后），
        # 导致所有管道表被判为非法、表格/图片抽取整体失效。
        if header_line_idx is None or header_line_idx + 1 >= len(lines) or \
                not _SEP_ROW_RE.match(lines[header_line_idx + 1].strip() or ""):
            # 不构成 GFM 表（无分隔行）→ 当作普通文本继续
            if stripped == "【表格】":
                # 【表格】后紧跟非法表：跳过锚点，避免死循环
                consumed.add(start)
            continue
        # 收集数据行（含表头与分隔行）
        rows_raw: list[list[str]] = []
        headers: list[str] = []
        j = header_line_idx
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = _split_pipe_row(lines[j])
            if not _SEP_ROW_RE.match(lines[j].strip()):
                rows_raw.append(cells)
            j += 1
        if rows_raw:
            headers = rows_raw[0]
            data_rows = rows_raw[1:]
        else:
            headers, data_rows = [], []
        page_num = page_of_line[header_line_idx]
        table_id = f"t{len(tables) + 1}"
        tbl = {
            "table_id": table_id,
            "page_num": page_num,
            "title": _extract_title(last_plain),
            "headers": headers,
            "rows": data_rows,
            # data 保留完整行列（含表头行），对齐规范 §2.2.2 的 data 字段
            "data": ([headers] if headers else []) + data_rows,
            "merged_cells": [],
            "source_ref": f"{doc_id}#page:{page_num}#table:{table_id}",
        }
        tables.append(tbl)
        _page_entry(page_num)["tables"].append(table_id)
        for k in range(start, j):
            consumed.add(k)
        last_plain = ""
        i = j

    # ---- 3. 图片与公式抽取 + 页文本组装 ----
    images: list[dict] = []
    formulas: list[dict] = []
    for num in sorted(pages):
        entry = pages[num]
        body_lines: list[str] = []
        for g_idx in entry["line_idx"]:
            ln = lines[g_idx]
            if g_idx in consumed and ln.strip().startswith("|"):
                # 表格行不重复入页文本（内容已结构化入 tables），留占位标记
                if not _SEP_ROW_RE.match(ln.strip()):
                    body_lines.append("> 〔表格见 tables 结构化数据〕")
                continue
            for m in _MD_IMG_RE.finditer(ln):
                img_id = f"img_{len(images) + 1:03d}"
                images.append({
                    "image_id": img_id,
                    "page_num": num,
                    "file_path": m.group(2),
                    "alt": m.group(1),
                    "caption": m.group(1),
                    "ocr_text": "",
                    "image_type": "embedded",
                    "confidence": None,
                    "source_ref": f"{doc_id}#page:{num}#img:{img_id}",
                })
                entry["images"].append(img_id)
            for m in _BRACKET_IMG_RE.finditer(ln):
                img_id = f"img_{len(images) + 1:03d}"
                pnum = int(m.group(2)) if m.group(2) else num
                images.append({
                    "image_id": img_id,
                    "page_num": pnum,
                    "file_path": m.group(1),
                    "alt": "",
                    "caption": "",
                    "ocr_text": "",
                    "image_type": "ocr",
                    "confidence": None,
                    "source_ref": f"{doc_id}#page:{pnum}#img:{img_id}",
                })
                _page_entry(pnum)["images"].append(img_id)
            cap = _CAPTION_RE.match(ln.strip())
            if cap and images and ln.strip():
                # 题注行（图2-1 xxx）回填到最近一张无题注图片
                for img in reversed(images):
                    if not img["caption"]:
                        img["caption"] = cap.group(1).strip()[:80]
                        break
            fm = _BLOCK_MATH_RE.search(ln)
            if fm:
                f_id = f"f{len(formulas) + 1}"
                formulas.append({
                    "formula_id": f_id, "page_num": num,
                    "latex": fm.group(1).strip(),
                    "source_ref": f"{doc_id}#page:{num}#formula:{f_id}",
                })
                entry["formulas"].append(f_id)
            body_lines.append(ln)
        entry["text"] = "\n".join(body_lines).strip()

    page_nums = sorted(pages)
    page_list = []
    for num in page_nums:
        e = pages[num]
        page_list.append({
            "page_num": num,
            "text": e["text"],
            "tables": e["tables"],
            "images": e["images"],
            "formulas": e["formulas"],
        })

    return {
        "doc_id": doc_id,
        "page_count": len(page_list),
        "pages": page_list,
        "tables": tables,
        "table_count": len(tables),
        "images": images,
        "image_count": len(images),
        "formulas": formulas,
        "formula_count": len(formulas),
    }


def wrap_parsed_markdown(doc_id: str, title: str, markdown: str,
                         page_count: int) -> str:
    """按规范 §2.2.1 给解析层 Markdown 加 front-matter 头。"""
    head = (
        "---\n"
        f"doc_id: {doc_id}\n"
        f"title: {title}\n"
        f"page_count: {page_count}\n"
        "---\n\n"
    )
    return head + markdown
