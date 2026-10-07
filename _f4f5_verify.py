# -*- coding: utf-8 -*-
"""F4/F5 验证：破折号长尾标题 + 空标题节点检出。"""
import sys
sys.path.insert(0, r"j:\编程\专项方案工具箱\backend")
from app.services import outline_quality as oq

outline = [
    {"id": "1", "title": "4 门窗安装工程", "children": [
        {"id": "1.1", "title": "4.1 门窗安装工程 — 成品门窗安装、玻璃安装及密封胶施工技术要求"},
    ]},
    {"id": "2", "title": "3 施工现场消防安全", "children": [
        {"id": "2.1", "title": "3.1 施工现场消防安全 —— 动火审批制度、装饰材料防火要求"},
    ]},
    {"id": "3", "title": "正常标题（含范围）", "children": [
        {"id": "3.1", "title": "   "},
        {"id": "3.2", "title": "3.2 正常子标题"},
    ]},
]

rep = oq.check_outline_continuity(outline)
print("issue_counts:", rep["issue_counts"])
print("dash:", rep["dash_titles"])
print("empty:", rep["empty_titles"])

ok = True
if len(rep["dash_titles"]) != 2:
    ok = False; print("[FAIL] 应检出 2 处破折号标题")
if len(rep["empty_titles"]) != 1 or rep["empty_titles"][0]["path"] != "3.1":
    ok = False; print("[FAIL] 应检出 1 处空标题，路径 3.1")
if rep["ok"]:
    ok = False; print("[FAIL] 存在问题时 ok 应为 False")

# 辅助函数单测
if not oq._has_dash_title("标题 — 长尾"):
    ok = False; print("[FAIL] _has_dash_title 单 em dash")
if oq._has_dash_title("普通标题-无空格"):
    ok = False; print("[FAIL] 不应误报无空格连字符")
if oq._has_dash_title("施工范围（一）"):
    ok = False; print("[FAIL] 不应误报正常标题")

print("结果:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
