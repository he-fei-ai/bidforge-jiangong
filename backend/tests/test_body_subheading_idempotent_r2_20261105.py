"""正文子标题规范化必须幂等（2026-11-05 · 第二轮编号漂移复查）。

为什么单独锁这条性质：``validate_section_content_numbering`` 的判定方式是
「对**已落库正文**再跑一遍规范化，若有改动即判不一致」。因此只要规范化
不是幂等的，「落库时规范化一次」的正文在复核/预检时就会被判成漂移 ——
这正是生产 22:20/22:23 复核 10/9/5 处、22:53 预检 3 章的**表现**。

本文件用「一次 → 二次 → 三次」逐轮比对，覆盖：
- 层级（# ~ ######、纯文本编号标题）
- 前缀形态（数字 / 字母 / 全角括号 / 加粗包裹）
- 有 DB 子章节（body 命名空间降级）与无子章节两种档
"""

import pytest
from app.services.numbering import renumber_section_body_subheadings

HEADINGS = [
    "1.1 施工准备",
    "2.3 验收要求",
    "1）材料进场",
    "a、吊顶施工",
    "（1）基层处理",
    "1) 测量放线",
    "**1.1 施工准备**",
    "一、总体安排",
    "1.1.1 细部节点",
]


def _content_for(section_level: int, headings: list[str]) -> str:
    lines = []
    for i, h in enumerate(headings):
        prefix = "#" * min(section_level + 1 + (i % 2), 6)
        lines.append(f"{prefix} {h}")
        lines.append("正文段落。" * 5)
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("section_number,level", [("1", 1), ("1.1", 2), ("2.3.1", 3)])
@pytest.mark.parametrize("has_children", [False, True])
@pytest.mark.parametrize("headings", [HEADINGS, HEADINGS[3:], ["1）材料进场", "a、吊顶施工"]])
def test_renumber_is_idempotent(section_number, level, has_children, headings):
    content = _content_for(level, headings)
    current = content
    seen = []
    for _ in range(3):
        current, changes = renumber_section_body_subheadings(
            current, section_number, level, "示例章节", has_db_children=has_children
        )
        seen.append(len(changes))
    assert seen[1] == 0, f"第二次规范化仍有 {seen[1]} 处改写（幂等被破坏），轮次={seen}"
    assert seen[2] == 0, f"第三次规范化仍有 {seen[2]} 处改写，轮次={seen}"
