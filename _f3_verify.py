# -*- coding: utf-8 -*-
"""F3 验证：半角方括号中文占位检出 + 开关回退 + 不误报。"""
import sys
sys.path.insert(0, r"j:\编程\专项方案工具箱\backend")
from app.services import placeholder_inventory as pi

content = (
    "就近送往 [就近综合医院] 救治，备选 [邻近专科医院/门诊部]。"
    "规范参数【待补充：搭设高度】缺失，模糊写法 xx 一处。"
    "英文引用 [ABC] 与下标 a[i] 不应误报，公式符号 [β] 无中文。"
)

hits_on = pi.scan_occurrences(content)
hits_off = pi.scan_occurrences(content, include_bracket=False)

kinds_on = [h["kind"] for h in hits_on]
bracket_fields = [h["field"] for h in hits_on if h["kind"] == "bracket"]
kinds_off = [h["kind"] for h in hits_off]

print("开启 kinds:", kinds_on)
print("bracket 字段:", bracket_fields)
print("关闭 kinds:", kinds_off)

ok = True
if kinds_on.count("bracket") != 2:
    ok = False; print("[FAIL] 应检出 2 处 bracket")
if bracket_fields != ["就近综合医院", "邻近专科医院/门诊部"]:
    ok = False; print("[FAIL] bracket 字段提取错误")
if "bracket" in kinds_off:
    ok = False; print("[FAIL] 关闭后不应有 bracket")
if any(h["field"] for h in hits_on if False):
    pass

# 聚合报告
rep = pi.build_placeholder_report([{"id": "s1", "title": "t", "content": content}])
print("bracket_total:", rep["bracket_total"], "total:", rep["total"])
if rep["bracket_total"] != 2:
    ok = False; print("[FAIL] 聚合 bracket_total 应为 2")

print("结果:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
