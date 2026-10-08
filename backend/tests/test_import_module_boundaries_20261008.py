# -*- coding: utf-8 -*-
"""解析提取（import）模块 —— 分块/结构化/字段/判据边界补测（2026-10-08）

与既有套件的分工：
- `test_import_module_crosscut_20261008.py` 已覆盖 告警序列化 / 编码回退链 /
  CSV 边界 / ZIP 闸门 / OLE 细分 / upload-outline 路由防御 / 分类契约；
- 本文件补齐其未覆盖的**纯函数边界**：
  1. doc_chunker：语义硬切（重叠连续性）/ 分页分块（页标记不入块文本）/
     表格独立成块 + 页关联 / chunk_id 唯一 / 前后链一致；
  2. md_structured：单列 GFM 表（D5 修复回归：``| --- |`` 与 ``|:-:|``）/
     超长标题候选丢弃 / 图片引用未知页归位（KeyError 修复回归）/ 题注回填；
  3. pipeline._field_value：规范键 + 历史别名 + 三要素 dict 取值语义；
  4. upload_outline._outline_seems_valid：畸形 children / 扁平阈值边界；
  5. file_parser.signature_valid：二进制签名矩阵 / 文本类不受限；
  6. ocr_bytes_sync：配置禁用时的可读报错（含启用方法）。
全部纯函数 / monkeypatch，无真实 AI、无磁盘依赖。
"""
from __future__ import annotations

import pytest

from app.services.doc_pipeline import doc_chunker as dc
from app.services.doc_pipeline import pipeline as dpl
from app.services.doc_pipeline.md_structured import parse_markdown_structured
from app.services import file_parser as fp
from app.routers import upload_outline as uo


# ---------------------------------------------------------------------------
# 1. doc_chunker：语义硬切与重叠连续性
# ---------------------------------------------------------------------------

def test_split_semantic_hard_split_pieces_bounded_and_overlapping():
    """2500 字单段：切成 4 片、每片 ≤800、相邻片保留 100 字重叠。"""
    text = "甲" * 2500
    pieces = dc._split_semantic(text)
    assert len(pieces) == 4
    assert all(len(p) <= dc.SEMANTIC_CHUNK_SIZE for p in pieces)
    assert pieces[0] == text[:dc.SEMANTIC_CHUNK_SIZE]
    overlap = text[dc.SEMANTIC_CHUNK_SIZE - dc.SEMANTIC_OVERLAP:
                   dc.SEMANTIC_CHUNK_SIZE]
    assert pieces[1].startswith(overlap)
    # 尾段非空且来自原文尾部
    assert pieces[-1] and pieces[-1] in text


def test_split_semantic_short_text_passthrough():
    """短文本不被切碎：单段原样返回。"""
    text = "短段落"
    assert dc._split_semantic(text) == [text]


def test_chunk_document_pages_headings_tables_and_chain():
    """页标记分块：标记不入块文本；表格独立成块；前后链与 id 唯一。"""
    md = (
        "<!-- page:1 -->\n"
        "# 第一章 工程概况\n\n"
        "本工程为装饰装修专项施工方案。\n\n"
        "<!-- page:2 -->\n"
        "## 1.1 主要机械\n\n"
        "【表格】\n"
        "| 名称 | 数量 |\n"
        "| --- | --- |\n"
        "| 塔吊 | 2 |\n"
    )
    structured = parse_markdown_structured(md, doc_id="d1")
    chunks = dc.chunk_document(md, doc_id="d1", structured=structured)

    assert chunks, "必须产出至少一个块"
    # id 唯一（重复 id 会让前端 Tree key 冲突）
    ids = [c["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids))
    # 页标记不得混入块正文
    for c in chunks:
        assert "<!-- page" not in c["text"]
    # 表格独立成块（chunk_type=table）且挂到表格 id
    table_chunks = [c for c in chunks if c["chunk_type"] == "table"]
    assert table_chunks and structured["tables"], "应识别出表格并独立成块"
    assert table_chunks[0]["tables"] == [structured["tables"][0]["table_id"]]
    # 第 2 页的块归属正确
    assert any(c["page_num"] == 2 for c in chunks)
    # 前后链一致
    for i, c in enumerate(chunks):
        assert c["prev_chunk_id"] == (chunks[i - 1]["chunk_id"] if i else None)
        assert c["next_chunk_id"] == (chunks[i + 1]["chunk_id"]
                                      if i + 1 < len(chunks) else None)


def test_chunk_row_of_serializes_extra_json_columns():
    """chunk dict → 插入行：tables/images/extra 全部可 JSON 序列化（无崩溃）。"""
    import json
    c = dc._new_chunk("dX", 0, text="正文", chunk_type="section")
    row = dc.chunk_row_of(c, created_at="2026-10-08T00:00:00")
    assert row[-1] == "2026-10-08T00:00:00"
    json.loads(row[11])   # tables 列
    json.loads(row[12])   # images 列
    json.loads(row[14])   # extra（formulas/offset）列


# ---------------------------------------------------------------------------
# 2. md_structured：单列 GFM 表 / 标题候选 / 图片未知页 / 题注
# ---------------------------------------------------------------------------

def test_md_single_column_table_with_two_dash_separator():
    """D5 回归：``| --- |`` 单列分隔行必须产出 1 列表格。"""
    md = "| 名称 |\n| --- |\n| 塔吊 |\n| 起重机 |\n"
    out = parse_markdown_structured(md, doc_id="d2")
    assert out["table_count"] == 1
    t = out["tables"][0]
    assert t["headers"] == ["名称"]
    assert t["rows"] == [["塔吊"], ["起重机"]]


def test_md_single_column_table_with_single_dash_and_alignment():
    """GFM 允许单横线 + 对齐冒号：``|:-:|`` 同样不得漏判。"""
    md = "| 数值 |\n|:-:|\n| 42 |\n"
    out = parse_markdown_structured(md, doc_id="d3")
    assert out["table_count"] == 1
    assert out["tables"][0]["headers"] == ["数值"]
    assert out["tables"][0]["rows"] == [["42"]]


def test_md_table_title_candidate_too_long_is_dropped():
    """超过 60 字的说明行不得成为表格标题。"""
    from app.services.doc_pipeline.md_structured import _extract_title
    assert _extract_title("字" * 61) == ""
    assert _extract_title("工程规模一览表") == "工程规模一览表"


def test_md_image_unknown_page_reference_falls_back_to_current_page():
    """图片引用不存在的页（page:99）不得虚增页数，按标记所在页归位。"""
    md = "<!-- page:1 -->\n正文一行\n[IMAGE: scan.png, page:99]\n"
    out = parse_markdown_structured(md, doc_id="d4")
    assert out["page_count"] == 1, "未知页引用不得把 page_count 顶到 99"
    assert out["images"] and out["images"][0]["page_num"] == 1


def test_md_caption_backfills_latest_uncaptioned_image():
    """题注行（图1-1 xxx）回填最近一张**无题注**图片（空 alt 的 ![]() 图片）。"""
    md = ("<!-- page:1 -->\n"
          "![](a.png)\n"
          "图1-1 施工平面布置图\n")
    out = parse_markdown_structured(md, doc_id="d5")
    assert out["images"], "应识别出 markdown 图片"
    assert not out["images"][0]["alt"]
    assert out["images"][0]["caption"].startswith("施工平面布置图")


def test_md_alt_text_serves_as_caption_so_later_caption_line_wins_nothing():
    """``![alt](src)`` 的 alt 本身即题注 —— 已有题注的图片不被后续题注行覆盖。"""
    md = ("<!-- page:1 -->\n"
          "![现场布置](b.png)\n"
          "图1-2 另一个题注\n")
    out = parse_markdown_structured(md, doc_id="d6")
    assert out["images"][0]["caption"] == "现场布置"


# ---------------------------------------------------------------------------
# 3. pipeline._field_value：规范键 + 历史别名 + 三要素 dict
# ---------------------------------------------------------------------------

def test_field_value_prefers_canonical_then_alias():
    vals = {"project_number": "", "project_code": "AJ-2026-01"}
    aliases = ("project_number", "project_code")
    assert dpl._field_value(vals, aliases) == "AJ-2026-01"


def test_field_value_unwraps_three_element_dict_and_ignores_empty():
    vals = {"construction_unit": {"value": "某建设集团"}}
    assert dpl._field_value(vals, ("construction_unit", "client")) == "某建设集团"
    # dict 内空串视同缺失
    vals2 = {"contractor": {"value": ""}}
    assert dpl._field_value(vals2, ("contractor",)) is None


def test_field_value_plain_string_passthrough():
    assert dpl._field_value({"project_name": "某工程"}, ("project_name",)) == "某工程"
    assert dpl._field_value({}, ("project_name",)) is None


# ---------------------------------------------------------------------------
# 4. upload_outline._outline_seems_valid：畸形与阈值边界
# ---------------------------------------------------------------------------

def test_outline_seems_valid_rejects_empty_and_tiny_flat():
    assert uo._outline_seems_valid([]) is False
    assert uo._outline_seems_valid([{"title": f"第{i}章"} for i in range(4)]) is False


def test_outline_seems_valid_accepts_branch_or_five_flat_nodes():
    assert uo._outline_seems_valid(
        [{"title": "第一章", "children": [{"title": "1.1"}]}]) is True
    assert uo._outline_seems_valid(
        [{"title": f"第{i}章"} for i in range(5)]) is True


def test_outline_seems_valid_malformed_children_not_counted_as_branch():
    """children 为字符串等畸形值时不得当作「有层级」（坏数据须走 AI 兜底）。"""
    bad = [{"title": "第一章", "children": "1.1"}]
    assert uo._outline_seems_valid(bad) is False
    # 顶层非 list 同样拒绝
    assert uo._outline_seems_valid("不是列表") is False


# ---------------------------------------------------------------------------
# 5. file_parser.signature_valid：签名矩阵
# ---------------------------------------------------------------------------

def test_signature_valid_matrix():
    assert fp.signature_valid("pdf", b"%PDF-1.7\n")
    assert not fp.signature_valid("pdf", b"MZ\x90\x00rest")
    assert fp.signature_valid("docx", b"PK\x03\x04rest")
    assert fp.signature_valid("docx", b"\xd0\xcf\x11\xe0rest")   # OLE 兼容
    assert not fp.signature_valid("png", b"GIF89a")
    # 文本类未登记 → 一律放行
    assert fp.signature_valid("txt", b"anything")
    assert fp.signature_valid("md", b"")
    # 未知/空扩展名放行（交由嗅探与二进制防护兜底）
    assert fp.signature_valid("", b"\x00\x01")


# ---------------------------------------------------------------------------
# 6. ocr_bytes_sync：配置禁用语义
# ---------------------------------------------------------------------------

def test_ocr_disabled_config_raises_actionable_error(monkeypatch):
    """OCR_ENABLED=false 时必须报「如何启用」，不得返回空文本假成功。"""
    from app.config import settings
    from app.services.ocr import OcrUnavailableError, ocr_bytes_sync
    monkeypatch.setattr(settings, "ocr_enabled", False, raising=False)
    with pytest.raises(OcrUnavailableError) as ei:
        ocr_bytes_sync(b"\x89PNG\r\n\x1a\nfake")
    assert "OCR_ENABLED" in str(ei.value) or "OCR_ENGINE" in str(ei.value)


def test_ocr_empty_input_returns_empty_result_without_engines():
    """空字节直接返回空结果（不抛、不探测引擎）。"""
    from app.services.ocr import OcrResult, ocr_bytes_sync
    r = ocr_bytes_sync(b"")
    assert isinstance(r, OcrResult) and not r
