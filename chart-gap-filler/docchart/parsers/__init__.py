# -*- coding: utf-8 -*-
"""解析器注册表：按扩展名分发，插件化入口。"""

from __future__ import annotations

from pathlib import Path

from ..models import Document
from .docx_parser import parse_docx
from .html_parser import parse_html
from .markdown_parser import parse_markdown

# 扩展名 -> 解析函数（后续注册 PDF 解析器只需在此追加）
_REGISTRY = {
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".html": parse_html,
    ".htm": parse_html,
    ".docx": parse_docx,
}


def supported_formats() -> list[str]:
    return sorted(_REGISTRY)


def register_parser(ext: str, fn) -> None:
    """插件扩展点：按扩展名注册新解析器（如 PDF），与 register_exporter 对称。"""
    key = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
    _REGISTRY[key] = fn


def parse_file(path: str | Path) -> Document:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文档不存在: {p}")
    parser = _REGISTRY.get(p.suffix.lower())
    if parser is None:
        raise ValueError(
            f"暂不支持的格式 {p.suffix}（已支持：{', '.join(supported_formats())}；PDF 为后续扩展）"
        )
    return parser(p)
