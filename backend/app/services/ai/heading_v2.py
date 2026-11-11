"""V6.0 增强版标题编号生成器（从 heading_standard.py 拆分）

HeadingNumberingGeneratorV2：实例化版，维护计数器+父级ID状态，支持八级标题，
每级在父级变更时自动重置。适用于流式生成场景：DOCX 导出、实时正文生成。

✅ 口径确认（2026-11-05）：**二级编号每章重置为 1 是既定正确行为**。
《L3以下“少目录、多正文”编制标准》只要求 L3 = L2.序号（如 1.1、11.1/11.2），
不要求 L2 全文连续；后者仅是标准文档示例中的一种写法。请勿把“L2 全文连续”
当作缺陷去改本类的重置语义（会影响导出样式、正文 subheading 与前端展示）。
"""

from __future__ import annotations

from app.services.ai.heading_templates import HEADING_MANAGED_LEVELS, HEADING_STYLE_CONFIG

# ✅ 编号统一（2026-09-25）：编号字符表唯一事实源收敛到 services/numbering.py，
#    类属性保留同名别名兼容既有引用（tests / 外部脚本按类属性访问）。
from app.services.numbering import ALPHABET as _NUMBERING_ALPHABET


def _alpha_label(n: int) -> str:
    """Excel 风格字母编号：1→a, 2→b, …, 26→z, 27→aa, 28→ab, …（R203）。

    ✅ R209（2026-11-10）：实现下沉到 ``services.numbering.alpha_label``
    （唯一事实源），本函数保留为薄包装 —— 与 heading_templates._alpha_label /
    content_blocks 的 L7 字母共用同一实现。
    """
    from app.services.numbering import alpha_label

    return alpha_label(n)
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
            # ✅ R209（2026-11-10 · L1 章号超 30 回退）：旧实现把 L1 章号**硬钳在
            #    30**（`min(30, c[0])`）⇒ 目录含 ≥31 个一级章时，第 31/32/… 章的
            #    标题**字面完全相同**（都是"第三十章"），且三章的正文子标题前缀
            #    全部塌缩为 "30" → 跨章撞号、交付文档目录页出现多个同名章节。
            #    现与 numbering.stored_id_to_display（:470）与前端
            #    outlineNumbering.formatOutlineTitle 同口径：字表内用中文数字，
            #    超出字表回退阿拉伯数字（第31章），**绝不产出重复章号**。
            n = max(1, c[0])
            if 0 < n < len(self.CHINESE_NUMBERS):
                return f"第{self.CHINESE_NUMBERS[n]}章"
            return f"第{n}章"
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
            # ✅ R203 FIX：旧实现钳制在 ALPHABET[-1]（z），超 26 个同级尾注静默
            #    折叠为 z。改为 Excel 风格双字母续编：27→aa, 28→ab, …
            return _alpha_label(c[6] or 1)
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
