# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"j:\编程\专项方案工具箱\backend")
from app.services import docx_math

cases = [
    "立杆轴向压力设计值 $N = $由支架立杆轴向力计算确定。",
    "式中 $N$ 为脚手架立杆轴向压力设计值。",
    "甲醛释放量控制应满足不超过 $L = $ 的目标值",
]
for c in cases:
    print("原文:", repr(c))
    for disp, latex, m in docx_math.iter_formulas(c):
        print("  命中公式:", disp, repr(latex))
        omml = docx_math.latex_to_omml(latex)
        print("  OMML:", omml)
    print()
