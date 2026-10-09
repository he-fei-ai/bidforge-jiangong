# -*- coding: utf-8 -*-
"""R50（2026-10-07）：导出体检新增「中英混杂」规则 + 表格无表题 WARNING。

取证来源：2026-10-07 第4轮交付文档（装饰装修专项施工方案）
- P389「签署 material acceptance records 后方可卸货入库」——AI 输出中英混杂，
  旧体检规则（placeholder/html_tag/bidding_terms/clipboard/latex）全部漏检。
- 24 个正文表格 0 表题 —— AI 未在表格上方写表名行，导出静默裸奔，无任何日志信号。
"""
import re


def test_mixed_language_detects_english_phrase_between_chinese():
    """中文句子里夹连续两个小写英文单词 → 命中。"""
    from app.routers.export import _AUDIT_RULES
    rule = next(r for r in _AUDIT_RULES if r[0] == "mixed_language")
    rx = rule[3]
    assert rx.search("材料进场验收合格后签署 material acceptance records 后方可卸货")
    assert rx.search("现场签署 work permit before operation 才能动火")


def test_mixed_language_does_not_false_positive_on_spec_codes():
    """规范编号（GB/JGJ 大写）、化学式（HCHO）、单个英文术语不误伤。"""
    from app.routers.export import _AUDIT_RULES
    rx = next(r for r in _AUDIT_RULES if r[0] == "mixed_language")[3]
    assert not rx.search("执行 GB 50325-2020 标准要求")
    assert not rx.search("甲醛 HCHO 浓度限值")
    assert not rx.search("现场材料分类堆放整齐")
    assert not rx.search("采用 PVC 卷材防水")
    assert not rx.search("按 ISO 9001 体系运行")
    assert not rx.search("签署 material acceptance records")  # 英文后无中文


def test_audit_content_flags_mixed_language():
    """端到端：audit_content 把中英混杂计入 items。"""
    from app.routers.export import audit_content
    audit = audit_content([
        {"title": "材料进场验收",
         "content": "检测合格并签署 material acceptance records 后方可卸货入库。"}
    ])
    keys = {it["key"]: it for it in audit["items"]}
    assert "mixed_language" in keys
    assert keys["mixed_language"]["count"] >= 1
    assert keys["mixed_language"]["level"] == "warn"


def test_audit_content_clean_document_no_mixed_language():
    from app.routers.export import audit_content
    audit = audit_content([
        {"title": "工程概况", "content": "本工程位于上海市虹口区，建筑面积约3200平方米。"}
    ])
    keys = {it["key"] for it in audit["items"]}
    assert "mixed_language" not in keys
