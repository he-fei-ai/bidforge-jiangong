# -*- coding: utf-8 -*-
"""docchart —— 文档图表缺失检测与自动补全系统（核心包）。

对外公共 API（惰性导入，避免 `import docchart` 即拉起 matplotlib）：
    from docchart import analyze, fill, save_report, Config

调用链路：parsers -> detection -> generation(datasource/recommender/renderer)
-> insertion(exporters) -> pipeline 校验，均以 models.Document/ChartGap 为数据契约。
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = ["analyze", "fill", "save_report", "Config", "supported_formats",
           "__version__"]


def __getattr__(name: str) -> Any:
    """按需暴露流水线入口，重依赖只在真正调用时加载。"""
    if name in ("analyze", "fill", "save_report"):
        from . import pipeline
        return getattr(pipeline, name)
    if name == "Config":
        from .config import Config
        return Config
    if name == "supported_formats":
        from .parsers import supported_formats as _sf
        return _sf
    raise AttributeError(f"module 'docchart' has no attribute {name!r}")
