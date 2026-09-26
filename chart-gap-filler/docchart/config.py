# -*- coding: utf-8 -*-
"""全局配置：字体、主题、输出尺寸、检测阈值。支持 YAML/JSON 文件覆盖默认值。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "render": {
        # 中文字体回退链（Windows 优先）
        "fonts": ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "sans-serif"],
        "dpi": 150,
        "figure_size": [8.0, 4.5],       # 常规统计图
        "flow_size": [8.0, 6.0],         # 流程图/关系图
        "palette": ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2",
                    "#59A14F", "#EDC948", "#B07AA1", "#9C755F"],
    },
    "detection": {
        "min_confidence": 0.35,          # 低于该置信度不进入报告
        "table_min_rows": 3,             # 至少几行数据才认为表格值得配图
        "table_min_numeric_cols": 1,     # 至少几个数值列
        "ref_window_blocks": 12,         # 引用与图表的匹配窗口（块数）
        "semantic_hint": True,           # 是否启用语义关键词建议（置信度较低）
    },
    "output": {
        "image_format": "png",           # png / svg（png 便于回插 docx）
        "image_dir": "charts",           # 导出文档旁的图片子目录
        "caption_prefix": "图",
        "auto_caption_suffix": "（AI 补全）",
    },
}


@dataclass
class Config:
    data: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG))

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        """加载配置文件（.yaml/.yml/.json），缺省用默认值；文件不存在时告警不报错。"""
        cfg = cls()
        if not path:
            return cfg
        p = Path(path)
        if not p.exists():
            return cfg
        text = p.read_text(encoding="utf-8")
        loaded: dict = {}
        if p.suffix in (".yaml", ".yml"):
            try:
                import yaml  # 可选依赖
                loaded = yaml.safe_load(text) or {}
            except ImportError:
                loaded = {}
        elif p.suffix == ".json":
            loaded = json.loads(text)
        # 深度合并两层即可（节 -> 键）
        for sec, kv in loaded.items():
            if isinstance(kv, dict):
                cfg.data.setdefault(sec, {})
                cfg.data[sec].update(kv)
            else:
                cfg.data[sec] = kv
        return cfg
