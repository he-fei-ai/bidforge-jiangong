# -*- coding: utf-8 -*-
"""R64 基线测量：时间轴「事件数 → 纸面毫米」真实曲线（改前）。

同 R60/R62 方法：真实渲染 → 读真实 PNG 尺寸（不传 scale 走默认 2.5），
再用 R62 落地的公共层 `paper_font_mm`（宽/高**双**上限）算纸面毫米。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _B)
for _k in ("ARCH_AUTO_SPLIT", "ARCH_LEAF_AUTO_VERTICAL", "TIMELINE_AUTO_WRAP"):
    os.environ.pop(_k, None)

from PIL import Image  # noqa: E402
from app.services.ai import mermaid_common as mc  # noqa: E402
from app.services.ai import mermaid_timeline as mt  # noqa: E402

# 各档字号（设计 px × scale=2.5 = 设备 px）
FONTS = {"标题20": 50, "名称15": 37, "日期12": 30, "描述11": 27}


def mm_of(w, h):
    """真实交付图：先按 `_fit_image_cm` 的缩放语义（这里渲染器不缩 ⇒ 1:1），再按双上限算。"""
    return {k: round(mc.paper_font_mm(v, w, h), 2) for k, v in FONTS.items()}


def ms(n):
    m = [{"name": f"里程碑{i + 1}", "date": f"2026-0{i % 9 + 1}-1{i % 9}",
          "desc": "说明", "status": "done"} for i in range(n)]
    s = mt._render_timeline_image_v2({"title": "测试", "milestones": m})
    if s is None:
        return None
    with Image.open(s) as im:
        w, h = im.size
    return w, h


print("事件数 | 画布(真实px)      | 名称/日期/描述 纸面mm        | 高上限是否已binding")
print("-" * 100)
for n in (3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 30):
    r = ms(n)
    if not r:
        print(f"{n:>5} | 渲染返回 None")
        continue
    w, h = r
    d = mm_of(w, h)
    binds = mc.paper_height_binds(w, h)
    print(f"{n:>5} | {w:>5}x{h:<4} | 名称 {d['名称15']:>5}  日期 {d['日期12']:>5}  "
          f"描述 {d['描述11']:>5}  标题 {d['标题20']:>5} | {'是' if binds else '否'}")
print("\n下限", mc.paper_font_min_mm(), "mm | scale=", mc.PAPER_SCALE_DEFAULT,
      "| 栏宽", mc.PAPER_COLUMN_WIDTH_CM, "cm | 高上限", mc.PAPER_MAX_HEIGHT_CM, "cm")
