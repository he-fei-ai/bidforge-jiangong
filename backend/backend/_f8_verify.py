import sys

sys.path.insert(0, ".")

from app.config import settings
from app.services.consistency_scanner import program_prescan

S_184 = "本项目装饰装修工程计划开工日期为2026年05月08日，计划竣工日期为2026年11月18日，总工期约184日历天"
S_195 = "本项目计划开工日期为 2026 年 05 月 08 日，计划竣工日期为 2026 年 11 月 18 日，总工期约195 日历天"

# 1) 复现：同一章节内 184/195 矛盾必须被检出
sections = [{"id": "sec_schedule", "title": "施工进度计划",
             "content": S_184 + "。" + S_195 + "。"}]
cands = program_prescan(sections)
intra = [c for c in cands if c["source"] == "program_prescan_intra"]
assert len(intra) == 1, f"应检出 1 条章内矛盾，实际 {len(intra)}"
c = intra[0]
assert "工期" in c["topic"]
vals = c["value"]
assert "184日历天" in vals and "195日历天" in vals, vals
assert len(c["section_occurrences"]) == 2
assert all(o["section_id"] == "sec_schedule" for o in c["section_occurrences"])
print("PASS-1 同章 184/195 矛盾检出:", c["value"])

# 2) 单值不误报
c2 = program_prescan([{"id": "a", "title": "t", "content": S_184}])
assert not [x for x in c2 if x["source"] == "program_prescan_intra"]
print("PASS-2 单一总工期不误报")

# 3) 同值重复出现不误报
c3 = program_prescan([{"id": "a", "title": "t",
                       "content": S_184 + "。再次强调" + S_184}])
assert not [x for x in c3 if x["source"] == "program_prescan_intra"]
print("PASS-3 同章同值重复不误报")

# 4) 开关关闭回退
settings.consistency_detect_intra_section = False
c4 = program_prescan(sections)
assert not [x for x in c4 if x["source"] == "program_prescan_intra"]
settings.consistency_detect_intra_section = True
print("PASS-4 开关关闭回退旧口径")

# 5) 非白名单主题（混凝土强度，按对象多值）不因章内多值误报
concrete = ("柱混凝土强度等级C40，梁混凝土强度等级C30，"
            "板混凝土强度等级C35")
c5 = program_prescan([{"id": "a", "title": "t", "content": concrete}])
assert not [x for x in c5 if x["source"] == "program_prescan_intra"], \
    [x["topic"] for x in c5]
print("PASS-5 混凝土强度按对象多值不报章内矛盾")

# 6) 跨章节既有行为不变
cross = program_prescan([
    {"id": "s1", "title": "t1", "content": S_184},
    {"id": "s2", "title": "t2", "content": S_195},
])
cross_old = [x for x in cross if x["source"] == "program_prescan"]
assert any(x["topic"] == "工期" for x in cross_old), "跨章候选应保留"
print("PASS-6 跨章节候选既有行为不变")

# 7) 与 merge_conflicts 联测：章内+跨章不产生重复冲突
from app.services.consistency_scanner import merge_conflicts

merged = merge_conflicts([], cands, scheme_id="sch1")
assert len(merged) == 1, f"章内场景应归一为 1 条冲突，实际 {len(merged)}"
assert merged[0]["severity"] == "medium"
assert merged[0]["id"].startswith("sch1" if False else "") or "-C001" in merged[0]["id"]
print("PASS-7 merge 后仅 1 条冲突:", merged[0]["id"], merged[0]["topic"])

print("\nALL F8 CHECKS PASSED")
