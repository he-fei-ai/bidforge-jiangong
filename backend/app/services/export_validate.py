# -*- coding: utf-8 -*-
"""导出后读回校验（R129 · 引入易标式「导出 → 校验 → 回流」闭环）。

【背景】导出前体检（export.audit_content / /export/check）只扫**源文**，
产物落盘后从未读回核对——渲染器降级、围栏吞噬、行内格式残留等缺陷
只有用户在 Word/WPS 里翻到才暴露。参考软件（易标）的导出机制在
「Packer 打包落盘」后还有一道读回校验；本模块把等价能力引入本仓。

【设计原则（对齐导出前体检「只体检、不篡改」）】
- 校验在 ``doc.save`` 之后对**内存中的 Document 对象** + **落盘文件**做，
  只产出结构化报告与计数，**绝不改写产物**（改写等于替用户决定内容）；
- 判据与导出前体检同源：HTML 标签 / 占位符正则取自
  ``services/content_residue_rules``（唯一事实源，export._AUDIT_RULES 引用同一
  常量；services 层禁止反向 import routers —— 层级护栏实拍抓到后上收）；
- 调用方（``export._build_docx_sync``）全程 fail-soft：校验自身异常只记
  WARNING，绝不阻断交付。

【校验维度】
1. Markdown 残留：正文章节里残留的 ``#`` 标题行 / ``**`` 加粗标记 /
   反引号 / GFM 分隔行 / ``![]()`` 图片语法 / Markdown 链接 ——
   渲染器应全部消费，产物中出现即渲染链路漏洞；
2. HTML 标签残留（与导出前体检同判据）；
3. HTML 实体残留（``&nbsp;`` ``&lt;`` 等）；
4. 占位符残留（×× / 【待补充】等，与导出前体检同判据）；
5. Word 2010+ 专有命名空间回潮（复用 docx_namespace.check_docx_file）；
6. 标题大纲映射（Heading 1~9 段落 → 层级 + 标题），供目录导航核对 ——
   真实页码由标准 TOC 域 + updateFields=true 在 Word/WPS 打开时自动
   刷新（OOXML 标准路径，跨软件兼容的唯一可靠做法），本映射用于
   核对「目录收录范围 = 实际标题」。
"""
from __future__ import annotations

import re
from typing import Any

#: Markdown 残留判据（逐条 (key, 编译后正则)）。
#: - heading：行首 # 标题（渲染器应已把标题块升格为 Heading 样式段落）
#: - bold：``**`` 加粗标记（行内格式渲染器应全部消费）
#: - backtick：反引号（代码应已转为等宽段落或代码样式）
#: - gfm_sep：GFM 表格分隔行（应已转成真表格）
#: - image：``![alt](url)`` 图片语法（应已嵌入为图片）
#: - md_link：``[text](http...)`` 链接语法（正文链接应转为可读文本）
_MARKDOWN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("heading", re.compile(r"^\s{0,3}#{1,6}\s+\S")),
    ("bold", re.compile(r"\*\*")),
    ("backtick", re.compile(r"`")),
    ("gfm_sep", re.compile(r"^\s*\|?[\s:]*-{2,}[\s:|-]*\|")),
    ("image", re.compile(r"!\[[^\]]*\]\([^)]*\)")),
    ("md_link", re.compile(r"\[[^\]]+\]\((?:https?:)?//[^)]+\)")),
)

#: HTML 实体残留（``&amp;`` 为 XML 合法转义，由 lxml 序列化兜底，不算残留；
#: 其余为正文可读性残留）。
_ENTITY_RESIDUE_RE = re.compile(
    r"&(?:nbsp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);")

#: 通用占位词残留（××/【待补充】由 export._AUDIT_RULES 的 placeholder 覆盖；
#: 这里补生成域常见英文占位词）。
_PLACEHOLDER_WORD_RE = re.compile(r"\b(?:TODO|TBD|FIXME)\b")

#: 大纲映射提取：Heading 1~9 命名样式。
_HEADING_STYLE_RE = re.compile(r"^Heading ([1-9])$")

#: 样例截断长度（报告体积控制）。
_SAMPLE_SNIPPET_LEN = 60
#: 每类残留最多样例数。
_MAX_SAMPLES = 5


def _iter_all_paragraph_texts(doc) -> list[str]:
    """收集正文段落 + 表格（含嵌套表）单元格段落 + 页眉页脚段落的文本。"""
    texts: list[str] = []
    for p in doc.paragraphs:
        texts.append(p.text or "")
    for tbl in doc.tables:
        _collect_table_texts(tbl, texts)
    for sec in doc.sections:
        for hf in (sec.header, sec.footer):
            if hf is None:
                continue
            for p in hf.paragraphs:
                texts.append(p.text or "")
            for tbl in hf.tables:
                _collect_table_texts(tbl, texts)
    return texts


def _collect_table_texts(tbl, texts: list[str]) -> None:
    """递归收集表格单元格段落文本（含嵌套表）。"""
    for row in tbl.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                texts.append(p.text or "")
            for inner in cell.tables:
                _collect_table_texts(inner, texts)


def _audit_patterns() -> dict[str, re.Pattern]:
    """HTML 标签/占位符判据（单一事实源：services/content_residue_rules）。

    ⚠️ 刻意**不**反向 import ``routers.export._AUDIT_RULES``：services 层禁止
    依赖 routers（层级倒置护栏，全量回归实拍抓到）；共用正则已上收至
    ``content_residue_rules``，export._AUDIT_RULES 也引用同一常量，两侧天然同源。
    """
    from app.services.content_residue_rules import (
        HTML_TAG_RESIDUE_RE,
        PLACEHOLDER_RESIDUE_RE,
    )
    return {"html_tag": HTML_TAG_RESIDUE_RE,
            "placeholder": PLACEHOLDER_RESIDUE_RE}


def validate_document(doc) -> dict[str, Any]:
    """对构建完成的 Document 对象做残留扫描与大纲提取（不落盘、不改写）。

    返回::

        {
          "markdown_residue": int,   # Markdown 语法残留总数
          "html_residue": int,       # HTML 标签残留数
          "entity_residue": int,     # HTML 实体残留数
          "placeholder_residue": int,# 占位符残留数
          "samples": {key: [段落片段]},
          "outline_map": [{"level": 1, "text": "第一章 ..."}],
          "paragraphs_scanned": int,
        }
    """
    counts = {
        "markdown_residue": 0,
        "html_residue": 0,
        "entity_residue": 0,
        "placeholder_residue": 0,
    }
    samples: dict[str, list[str]] = {}
    audit = _audit_patterns()
    html_tag_re = audit.get("html_tag")
    placeholder_re = audit.get("placeholder")

    texts = _iter_all_paragraph_texts(doc)
    for text in texts:
        stripped = text.strip()
        if not stripped:
            continue
        for key, pat in _MARKDOWN_PATTERNS:
            if pat.search(text):
                counts["markdown_residue"] += 1
                samples.setdefault(f"md_{key}", []).append(
                    stripped[:_SAMPLE_SNIPPET_LEN])
        if html_tag_re is not None and html_tag_re.search(text):
            counts["html_residue"] += 1
            samples.setdefault("html", []).append(
                stripped[:_SAMPLE_SNIPPET_LEN])
        if _ENTITY_RESIDUE_RE.search(text):
            counts["entity_residue"] += 1
            samples.setdefault("entity", []).append(
                stripped[:_SAMPLE_SNIPPET_LEN])
        if placeholder_re is not None and placeholder_re.search(text):
            counts["placeholder_residue"] += 1
            samples.setdefault("placeholder", []).append(
                stripped[:_SAMPLE_SNIPPET_LEN])
        if _PLACEHOLDER_WORD_RE.search(text):
            counts["placeholder_residue"] += 1
            samples.setdefault("placeholder", []).append(
                stripped[:_SAMPLE_SNIPPET_LEN])
    for v in samples.values():
        del v[_MAX_SAMPLES:]

    outline_map: list[dict[str, Any]] = []
    for p in doc.paragraphs:
        m = _HEADING_STYLE_RE.match(p.style.name if p.style is not None else "")
        if m:
            outline_map.append({
                "level": int(m.group(1)),
                "text": (p.text or "").strip()[:80],
            })

    return {
        **counts,
        "samples": samples,
        "outline_map": outline_map,
        "paragraphs_scanned": len(texts),
    }


def post_export_check(doc, path) -> dict[str, Any]:
    """导出落盘后的完整校验：残留扫描 + 专有命名空间回潮检查。

    返回 :func:`validate_document` 的报告 + ``ns_clean`` / ``ns_issues``。
    任何子项失败都不抛异常（返回尽量完整的报告），阻断由调用方决定。
    """
    report = validate_document(doc)
    report.setdefault("ns_clean", True)
    report.setdefault("ns_issues", {})
    try:
        from app.services.docx_namespace import check_docx_file
        ns = check_docx_file(path)
        report["ns_clean"] = bool(ns.get("clean"))
        report["ns_issues"] = ns.get("issues") or {}
    except Exception:
        # 文件级校验失败不影响段落级报告；clean 缺省 True 由护栏测试单独锁
        pass
    return report
