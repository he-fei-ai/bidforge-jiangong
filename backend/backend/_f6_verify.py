import sys

sys.path.insert(0, ".")

from app.config import settings
from app.services.content_checkpoint import (
    build_chapter_element_block,
    chapter_required_elements,
)
from app.services.outline_checkpoint import build_outline_element_block
from app.services.scheme_classification import required_fields_for_chapter

GROUP_KEY = "专项应急预案按事故类型分组展开"

# 1) 默认开启：emergency 注入要素含分组结构
elems = chapter_required_elements("emergency", "装饰装修专项施工方案")
assert any(GROUP_KEY in e for e in elems), "默认应含分组结构要素"
print("PASS-1 默认 emergency 要素含分组结构，要素数 =", len(elems))

# 2) 原 5 项通用要素仍在，且分组要素排在它们之后（只追加不改原清单）
for f in ["应急组织架构", "应急联系人及电话", "应急物资清单", "救援线路", "附近医院信息"]:
    assert f in elems, f"原通用要素丢失: {f}"
grp_idx = next(i for i, e in enumerate(elems) if GROUP_KEY in e)
assert grp_idx >= 5, "分组要素应追加在原 5 项之后"
print("PASS-2 原 5 项通用要素保留，分组要素追加于其后")

# 3) 开关关闭：回退，不含分组要素
settings.emergency_group_by_accident_type = False
elems_off = chapter_required_elements("emergency")
assert not any(GROUP_KEY in e for e in elems_off), "关闭后不应含分组要素"
assert len(elems_off) == 5, f"关闭后应回到 5 项，实际 {len(elems_off)}"
print("PASS-3 开关关闭回退到 5 项旧口径")
settings.emergency_group_by_accident_type = True

# 4) 校验层 required_fields_for_chapter 不被污染（长句不进覆盖率差分校验）
rf = required_fields_for_chapter("emergency")
assert not any(GROUP_KEY in f for f in rf), "校验层不应含分组长句要素"
assert len(rf) == 5
print("PASS-4 校验层 base_fields 仍为 5 项，未被污染")

# 5) 其它章节不受影响
for key in ["overview", "basis", "schedule", "guarantee"]:
    assert not any(GROUP_KEY in e for e in chapter_required_elements(key)), f"{key} 被误伤"
print("PASS-5 其它章节不受影响")

# 6) 正文块与目录块均渲染出该结构要素
body = build_chapter_element_block("emergency", include_elements=True)
assert GROUP_KEY in body
ol = build_outline_element_block(include_elements=True)
assert GROUP_KEY in ol, "目录侧应同步注入"
print("PASS-6 正文块 / 目录块均渲染分组结构要素")

# 7) 总开关关闭时目录块仍为空字符串（既有行为不变）
assert build_outline_element_block(include_elements=False) == ""
print("PASS-7 content_chapter_elements_inject=False 时不注入（既有行为）")

print("\nALL F6 CHECKS PASSED")
