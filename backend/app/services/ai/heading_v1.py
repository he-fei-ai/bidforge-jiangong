"""V3.1 七级标题编号生成器（从 heading_standard.py 拆分）

HeadingNumberingGenerator：静态方法版，支持七级标题编号生成、反向检测、内容规范化。
依赖 heading_templates 中的共享常量与模板系统。
"""
from __future__ import annotations

import re

from app.services.ai.heading_templates import (
    HEADING_MANAGED_LEVELS, CHINESE_NUMBERS, HEADING_STYLE_CONFIG,
    HEADING_REGEX_PATTERNS, format_heading_by_id, format_outline_number_by_template,
    get_heading_template,
)


class HeadingNumberingGenerator:
    """V3.1 七级标题编号生成器（商业级）

    使用示例：
        counters, number, formatted = HeadingNumberingGenerator.next_heading(3, counters, "编制依据与适用标准")
        level, pure_title = HeadingNumberingGenerator.detect_level("1.1、设计依据")
    """

    @classmethod
    def generate_number(cls, level: int, counters: list[int]) -> str:
        if not 1 <= level <= 8:
            return ""
        if level not in HEADING_MANAGED_LEVELS:
            return ""
        if level == 1:
            n = max(1, min(30, counters[0] or 1))
            return f"第{CHINESE_NUMBERS[n]}章"
        if level == 2:
            return f"{counters[1] or 1}"
        if level == 3:
            return f"{counters[1] or 1}.{counters[2] or 1}"
        if level == 4:
            return f"{counters[1] or 1}.{counters[2] or 1}.{counters[3] or 1}"
        if level == 5:
            # ✅ 2026-09-23 对齐：L5 由旧「（X）」改为十进制 X.X.X.X
            #    （与 V2 / HEADING_STYLE_CONFIG / 前端树一致），format_heading 补顿号。
            return (
                f"{counters[1] or 1}.{counters[2] or 1}."
                f"{counters[3] or 1}.{counters[4] or 1}"
            )
        return ""

    @classmethod
    def format_heading(cls, level: int, counters: list[int], title: str) -> str:
        if not title or not title.strip():
            return title or ""
        if not 1 <= level <= 8:
            return title
        if level not in HEADING_MANAGED_LEVELS:
            return title
        number = cls.generate_number(level, counters)
        config = HEADING_STYLE_CONFIG[level]
        punct = config.get("punctuation", "")
        return f"{number}{punct}{title}"

    @classmethod
    def next_heading(cls, level: int, counters: list[int], title: str) -> tuple[list[int], str, str]:
        if not 1 <= level <= 8:
            return counters, "", title or ""
        new_counters = list(counters)
        level_idx_map = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
        increment_idx = level_idx_map[level]
        new_counters[increment_idx] = (new_counters[increment_idx] or 0) + 1
        reset_start = increment_idx + 1
        for i in range(reset_start, 8):
            new_counters[i] = 0
        for i in range(level_idx_map[level]):
            if not new_counters[i]:
                new_counters[i] = 1
        if level not in HEADING_MANAGED_LEVELS:
            return new_counters, "", title or ""
        number = cls.generate_number(level, new_counters)
        formatted = cls.format_heading(level, new_counters, title)
        return new_counters, number, formatted

    @classmethod
    def detect_level(cls, text: str) -> tuple[int, str]:
        if not text or not text.strip():
            return 0, text or ""
        stripped = text.strip()
        for pattern, level, group_idx in HEADING_REGEX_PATTERNS:
            m = re.match(pattern, stripped)
            if m:
                if level in (6,) and m.lastindex and m.lastindex >= group_idx:
                    pure = m.group(group_idx).strip()
                    pure = re.sub(r"^[、\s]+", "", pure)
                elif m.lastindex and m.lastindex >= group_idx:
                    pure = m.group(group_idx).strip()
                else:
                    pure = stripped
                return level, pure
        return 0, text

    @classmethod
    def get_style(cls, level: int) -> dict:
        return HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])

    @classmethod
    def get_all_levels(cls) -> list[int]:
        return [1, 2, 3, 4, 5, 6, 7]

    @classmethod
    def normalize_content(cls, content: str) -> str:
        if not content:
            return content
        SKIP_PREFIXES = ("<<", "[[", "```", "    ", "\t", "> ")
        out_lines: list[str] = []
        counters: list[int] = [0] * 8
        _LEVEL_IDX_MAP = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
        for raw_line in content.split("\n"):
            line = raw_line.rstrip()
            if not line.strip():
                out_lines.append(line)
                continue
            stripped = line.lstrip()
            if any(stripped.startswith(p) for p in SKIP_PREFIXES):
                out_lines.append(line)
                continue
            if stripped.startswith(("-", "·", "•", "*")) and not cls._looks_like_heading(stripped):
                out_lines.append(line)
                continue
            if re.match(r"^\d+[.\)）、]\s+", stripped) and not cls._looks_like_heading(stripped):
                out_lines.append(line)
                continue
            level, pure_title = cls.detect_level(stripped)
            if level == 0 or not pure_title:
                out_lines.append(line)
                continue
            increment_idx = _LEVEL_IDX_MAP[level]
            for i in range(increment_idx + 1, 8):
                counters[i] = 0
            counters[increment_idx] = (counters[increment_idx] or 0) + 1
            for i in range(increment_idx):
                if not counters[i]:
                    counters[i] = 1
            if level not in HEADING_MANAGED_LEVELS:
                out_lines.append(line)
                continue
            formatted = cls.format_heading(level, counters, pure_title)
            indent = line[: len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}{formatted}")
        return "\n".join(out_lines)

    @staticmethod
    def _looks_like_heading(text: str) -> bool:
        t = text.strip()
        if not t:
            return False
        if re.search(r"[。！？.!?]\s*$", t):
            return False
        if re.match(r"^第[一二三四五六七八九十百千零\d]+[章节]", t):
            return True
        if re.match(r"^[（(][一二三四五六七八九十百]+[)）]", t):
            if re.search(r"[。！？.!?]", t):
                return False
            return len(t) <= 60
        if re.match(r"^\d+\.\d+\.\d+", t):
            return True
        if re.match(r"^\d+\.\d+\s*[、．.\s]", t):
            return True
        if re.match(r"^[一二三四五六七八九十百千]+\s*[、．.]", t):
            return len(t) <= 60
        if re.match(r"^\d+\s*[、．.]", t):
            if re.search(r"[。！？.!?]", t):
                return False
            return len(t) <= 60
        if re.match(r"^\d+[）)]\s*", t):
            if re.search(r"[。！？.!?]", t):
                return False
            return len(t) <= 60
        if re.match(r"^[a-z]\s*[、．.]", t):
            if re.search(r"[。！？.!?]", t):
                return False
            return len(t) <= 60
        return False


# ============================================================
# 基于 ID 路径的模板化编号方法（绑定到 HeadingNumberingGenerator）
# ============================================================
def _generate_number_by_id(cls, id_str: str, level: int) -> str:
    config = get_heading_template(level)
    template = config.get("numbering_template", "{tail}")
    return format_outline_number_by_template(id_str, template)


def _format_heading_by_id(cls, id_str: str, level: int, title: str) -> str:
    return format_heading_by_id(id_str, level, title)


HeadingNumberingGenerator.generate_number_by_id = classmethod(_generate_number_by_id)
HeadingNumberingGenerator.format_heading_by_id = classmethod(_format_heading_by_id)
