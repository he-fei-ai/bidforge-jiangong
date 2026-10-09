import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services.numbering import (
    strip_outline_numbering, renumber_outline_nodes,
    renumber_section_body_subheadings, stored_id_to_display, stored_id_to_prefix,
)

print("== strip_outline_numbering ==")
cases = [
    "第一章 工程概况", "2.1 相关法律法规", "1.2.3（1）细部构造",
    "2.4.1钢筋工程", "2层作业平台", "10个人", "2023 年度安全生产计划",
    "1.1", "（三）施工准备", "1）编制依据", "3D打印技术", "十二层平面布置",
    "1.2.3.4 深层标题", "第 1 章 概述", "2、施工安排", "3 施工准备",
    "表3-1 主要设备表", "5.5.5.5.5.5.5.5.5 超深", "1.5m 深基坑",
]
for c in cases:
    print(f"  {c!r:40} -> {strip_outline_numbering(c)!r}")

print("\n== renumber_outline_nodes (cycle) ==")
a = {"title": "A", "children": []}
b = {"title": "B", "children": [a]}
a["children"].append(b)  # cycle a->b->a
try:
    out = renumber_outline_nodes([a], strip_titles=True)
    print("  ids:", out[0]["id"], [c.get("id") for c in out[0]["children"]],
          [c.get("id") for c in (out[0]["children"][0]["children"] if out[0]["children"] else [])])
except Exception as e:
    print("  EXC:", type(e).__name__, e)

print("\n== shared subtree (DAG, not cycle) ==")
leaf = {"title": "共享叶子"}
root1 = {"title": "R1", "children": [leaf]}
root2 = {"title": "R2", "children": [leaf]}
out = renumber_outline_nodes([root1, root2])
print("  root1 child id:", out[0]["children"][0]["id"])
print("  root2 child id:", out[1]["children"][0]["id"], "(should be 2.1, leaf reused across siblings)")

print("\n== renumber_section_body_subheadings ==")
content = """本节概述

2 资源配置
2.1 劳动力
一些正文
2.1.1 明细
7.3.3.1.1 材料计划
"""
newc, changes = renumber_section_body_subheadings(content, "3.2", 2, "施工计划")
print("  prefix(3.2)=", repr(stored_id_to_prefix("3.2")), "display=", repr(stored_id_to_display("3.2")))
for ch in changes:
    print(f"    L{ch['line']}: {ch['old']!r} -> {ch['new']!r}")
print("  --- output ---")
print(newc)

print("\n== stored id edge (D-8a) ==")
for v in ["3.5", "abc1.2", 3, "0.1", "3.0", None, True, [1]]:
    try:
        print(f"  {v!r}: display={stored_id_to_display(v)!r} prefix={stored_id_to_prefix(v)!r}")
    except Exception as e:
        print(f"  {v!r}: EXC {type(e).__name__} {e}")
