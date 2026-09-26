# -*- coding: utf-8 -*-
"""边界感知长文本截断单测（对齐 OpenBidKit userTextSplitter 思路，2026-09-22）"""
import pytest

from app.utils.text_splitter import truncate_to_boundary


def test_no_truncation_when_short():
    """未超上限：原样返回（零改动，向后兼容）。"""
    text = "这是一段很短的正文。"
    assert truncate_to_boundary(text, 100) == text


def test_truncate_at_paragraph_boundary():
    """优先在段落空行处截断。"""
    text = "第一段内容。\n\n第二段内容很长很长很长很长很长很长很长很长很长很长很长很长很长很长。"
    out = truncate_to_boundary(text, 20)
    assert out == "第一段内容。\n\n", out


def test_truncate_at_newline_when_no_paragraph():
    text = "第一行内容\n第二行内容很长很长很长很长很长很长很长很长很长很长很长很长很长很长。"
    out = truncate_to_boundary(text, 15)
    assert out.endswith("\n"), out
    assert len(out) <= 15


def test_truncate_at_sentence_punctuation():
    text = "第一句结束。第二句内容很长很长很长很长很长很长很长很长很长很长很长很长很长很长。"
    out = truncate_to_boundary(text, 12)
    assert out.endswith("。"), out
    assert len(out) <= 12


def test_truncate_at_comma():
    text = "甲方，乙方，丙方，丁方很长很长很长很长很长很长很长很长很长很长很长很长很长很长。"
    out = truncate_to_boundary(text, 10)
    assert out.endswith("，"), out
    assert len(out) <= 10


def test_hard_truncate_without_boundary():
    """无任何自然边界的超长 token：硬截断到上限。"""
    text = "x" * 200
    out = truncate_to_boundary(text, 50)
    assert out == "x" * 50


def test_boundary_not_exceeding_limit():
    """截断结果长度必须 <= 上限。"""
    text = "A。B。C。D。E。F。G。H。" + "很长很长很长很长很长很长很长很长很长很长很长很长很长很长。"
    for limit in (5, 13, 24, 40):
        out = truncate_to_boundary(text, limit)
        assert len(out) <= limit, (limit, len(out), out)


def test_max_len_non_positive_returns_empty():
    """m5 守卫：max_len<=0 返回空串（旧负值会越契约返回超长前缀）。"""
    assert truncate_to_boundary("很长的内容", 0) == ""
    assert truncate_to_boundary("很长的内容", -5) == ""


def test_none_and_illegal_inputs_no_crash():
    """m5 守卫：None/非字符串/非法 max_len 不抛异常，不返回 None（避免下游占位符残留）。"""
    assert truncate_to_boundary(None, 100) == ""
    assert truncate_to_boundary("abc", "x") == ""      # 非法 max_len → 视为 0
    assert truncate_to_boundary("abc", None) == ""      # 非法 max_len → 视为 0
    assert truncate_to_boundary(12345, 3) == "123"     # 非字符串 text → str() 后截断
