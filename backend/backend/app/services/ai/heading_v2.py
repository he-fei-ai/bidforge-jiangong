"""V6.0 增强版标题编号生成器（从 heading_standard.py 拆分）

HeadingNumberingGeneratorV2：实例化版，维护计数器+父级ID状态，支持八级标题，
每级在父级变更时自动重置。适用于流式生成场景：DOCX 导出、实时正文生成。
依赖 heading_templates 中的共享常量。
"""
from __future__ import annotations

from app.services.ai.heading_templates import HEADING_MANAGED_LEVELS, HEADING_STYLE_CONFIG

# ✅ 编号统一（2026-09-25）：编号字符表唯一事实源收敛到 services/numbering.py，
#    类属性保留同名别名兼容既有引用（tests / 外部脚本按类属性访问）。
from app.services.numbering import ALPHABET as _NUMBERING_ALPHABET
from app.services.numbering import CHINESE_NUMBERS as _NUMBERING_CHINESE_NUMBERS


class HeadingNumberingGeneratorV2:
    """多级标题编号生成器（V6.0 增强版） - 支持八级标题，每级在父级变更时自动重置

    使用示例：
        gen = HeadingNumberingGeneratorV2()
        num = gen.update_counter(1, "root")       # → "第一章"
        num = gen.update_counter(2, "ch1")        # → "1"
        num = gen.update_counter(1, "root")       # → "第二章"（二级自动重置）
        num = gen.update_counter(2, "ch2")        # → "1"（已重置）
    """

    # 编号字符表别名（唯一事实源：services/numbering.py）
    CHINESE_NUMBERS = _NUMBERING_CHINESE_NUMBERS
    ALPHABET = _NUMBERING_ALPHABET

    def __init__(self):
        self.counters = [0, 0, 0, 0, 0, 0, 0, 0]
        self.parent_ids: list[str | None] = [None, None, None, None, None, None, None, None]

    def update_counter(self, level: int, parent_id: str) -> str:
        if not 1 <= level <= 8:
            return ""
        level_idx_map = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
        idx = level_idx_map[level]
        if self.parent_ids[idx] != parent_id:
            self.counters[idx] = 0
            self.parent_ids[idx] = parent_id
            for i in range(idx + 1, 8):
                self.counters[i] = 0
                self.parent_ids[i] = None
        self.counters[idx] += 1
        if level not in HEADING_MANAGED_LEVELS:
            return ""
        return self._format_number(level)

    def _format_number(self, level: int) -> str:
        """按 2026-09 规范输出编号：
        L1 第一章  /  L2 1  /  L3 1.1  /  L4 1.1.1  /  L5 1.1.1.1
        L6 1）      L7 a
        """
        c = self.counters
        if level == 1:
            n = max(1, min(30, c[0]))
            return f"第{self.CHINESE_NUMBERS[n]}章"
        elif level == 2:
            return f"{c[1] or 1}"
        elif level == 3:
            return f"{c[1] or 1}.{c[2] or 1}"
        elif level == 4:
            return f"{c[1] or 1}.{c[2] or 1}.{c[3] or 1}"
        elif level == 5:
            # 十进制延续：1.1.1.1
            return f"{c[1] or 1}.{c[2] or 1}.{c[3] or 1}.{c[4] or 1}"
        elif level == 6:
            # 括号数字：1）、2）、（每父级重置）
            return f"{c[5] or 1}）"
        elif level == 7:
            # 小写字母：a、b、c、（每父级重置）
            idx = max(0, (c[6] or 1) - 1)
            return self.ALPHABET[min(idx, len(self.ALPHABET) - 1)]
        return ""

    def format_heading(self, level: int, title: str, parent_id: str) -> str:
        number = self.update_counter(level, parent_id)
        if not number:
            return title
        config = HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])
        punct = config.get("punctuation", "")
        return f"{number}{punct}{title}"

    def reset(self):
        self.counters = [0, 0, 0, 0, 0, 0, 0, 0]
        self.parent_ids = [None, None, None, None, None, None, None, None]
