"""Mermaid 图表渲染公共工具：字体、文本绘制、300 DPI 装饰器、Gantt 坐标。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
任何图表子模块（flowchart / gantt / ...）应从本模块导入共享工具。
"""
from __future__ import annotations

import functools
import logging
import os
import sys
from io import BytesIO

try:
    from PIL import Image, ImageFont
except ImportError:
    raise ImportError(
        "PIL/Pillow 未安装，Mermaid图表渲染功能不可用。"
        "请运行: pip install Pillow"
    )


logger = logging.getLogger(__name__)


# 字体路径配置（可被环境变量 FONT_PATH 覆盖）
_DEFAULT_FONT_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:\\Windows\\Fonts\\msyh.ttc",
    "C:\\Windows\\Fonts\\msyhbd.ttc",
    "C:\\Windows\\Fonts\\simsun.ttc",
    "C:\\Windows\\Fonts\\simhei.ttf",
    "C:\\Windows\\Fonts\\yahei.ttf",
    "C:\\Windows\\Fonts\\deng.ttf",
    "C:\\Windows\\Fonts\\FZSTK.TTF",
    "C:\\Windows\\Fonts\\FZHTK.TTF",
]

_DEFAULT_BOLD_FONT_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:\\Windows\\Fonts\\msyhbd.ttc",
    "C:\\Windows\\Fonts\\simhei.ttf",
    "C:\\Windows\\Fonts\\msyh.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]


def _resolve_font_paths(bold: bool = False) -> list[str]:
    """解析字体路径列表，优先级：环境变量 > 内置默认值

    环境变量 FONT_PATH 可指定一个或多个字体路径（分号分隔），
    若该环境变量存在则优先使用其中的路径；
    否则使用内置的 _DEFAULT_FONT_PATHS / _DEFAULT_BOLD_FONT_PATHS。
    """
    env_fonts = os.environ.get("FONT_PATH", "").strip()
    if env_fonts:
        return [p.strip() for p in env_fonts.split(";") if p.strip()]
    if bold:
        # 粗体优先列表 + 常规列表作为后备
        return _DEFAULT_BOLD_FONT_PATHS + _DEFAULT_FONT_PATHS
    return list(_DEFAULT_FONT_PATHS)

def _auto_detect_chinese_font() -> list[str]:
    """自动检测系统中可用的中文字体，返回发现的字体路径列表。

    在 Windows 上扫描 C:\\Windows\\Fonts 下常见中文字体模式；
    在 macOS 上扫描 /System/Library/Fonts 和 ~/Library/Fonts；
    在 Linux 上扫描 /usr/share/fonts 下的常见路径。
    """
    detected = []
    if sys.platform == "win32":
        font_dir = "C:\\Windows\\Fonts"
        patterns = ["msyh*", "simsun*", "simhei*", "yahei*", "deng*", "FZST*", "FZHT*", "mf*"]
        if os.path.isdir(font_dir):
            for pat in patterns:
                try:
                    from glob import glob

                    matched = glob(os.path.join(font_dir, pat))
                    detected.extend(matched)
                except Exception:
                    continue
    elif sys.platform == "darwin":
        search_dirs = [
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            os.path.expanduser("~/Library/Fonts"),
        ]
        for d in search_dirs:
            if os.path.isdir(d):
                try:
                    for f in os.listdir(d):
                        if any(
                            kw in f.lower()
                            for kw in ["stheit", "songti", "pingfang", "noto", "cjk"]
                        ):
                            detected.append(os.path.join(d, f))
                except Exception:
                    continue
    else:  # Linux
        search_dirs = ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")]
        for d in search_dirs:
            if os.path.isdir(d):
                try:
                    for root, dirs, files in os.walk(d):
                        for f in files:
                            if any(
                                kw in f.lower()
                                for kw in ["noto", "cjk", "wqy", "droid", "chinese", "hans", "sc"]
                            ):
                                detected.append(os.path.join(root, f))
                except Exception:
                    continue
    return detected

# 字体路径解析缓存：key = (是否粗体, FONT_PATH 环境变量原文) → 可用字体路径 or None
# ✅ 性能修复：默认字体列表是**跨平台**的（macOS/Linux/Windows 混排），本地机器上
#    排在最前的若干路径必然不存在，`_image_font` 每次调用都要逐个 `ImageFont.truetype`
#    触发 OSError 才能找到本机字体（实测 400 次调用 1.95s，是直接加载的 20 倍）。
#    渲染一张图要取 6~7 个字号，批量导出时开销可观。此处只探测一次并记忆结果，
#    后续调用直接命中本机路径（不再重复线性探测/目录扫描）。
_FONT_PATH_CACHE: dict[tuple[bool, str], "str | None"] = {}


def _resolve_working_font_path(bold: bool = False) -> "str | None":
    """解析并**记忆**首个可用的字体路径（跨平台列表逐个探测只做一次）。

    Returns:
        可用字体文件路径；全部不可用时返回 None（调用方回退 PIL 默认字体）。
    """
    env_fonts = os.environ.get("FONT_PATH", "").strip()
    cache_key = (bold, env_fonts)
    if cache_key in _FONT_PATH_CACHE:
        return _FONT_PATH_CACHE[cache_key]

    candidates: list[str] = []
    candidates.extend(_resolve_font_paths(bold=bold))
    resolved: "str | None" = None

    def _probe(paths: list[str]) -> "str | None":
        for fp in paths:
            if not fp:
                continue
            try:
                # 用极小字号探测可读性（成功即认为可用；不缓存字体对象，
                # 避免多线程下共享 FreeType 句柄的潜在风险）
                ImageFont.truetype(fp, 12)
                return fp
            except OSError:
                continue
        return None

    resolved = _probe(candidates)
    if resolved is None:
        # 自动检测中文字体（兜底搜索，仅首次失败时才付出目录扫描代价）
        resolved = _probe(_auto_detect_chinese_font())
        if resolved:
            logger.info("_image_font: 自动检测到中文字体: %s", resolved)

    _FONT_PATH_CACHE[cache_key] = resolved
    return resolved


def _image_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None

    # 1) 命中记忆的本机字体路径（避免每次线性探测跨平台列表）
    font_path = _resolve_working_font_path(bold=bold)
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            # 字体文件在运行期被移除/损坏：让缓存失效并重新探测一次
            _FONT_PATH_CACHE.pop((bold, os.environ.get("FONT_PATH", "").strip()), None)
            font_path = _resolve_working_font_path(bold=bold)
            if font_path:
                try:
                    return ImageFont.truetype(font_path, size)
                except OSError:
                    pass

    # 2) 最后降级：PIL 默认字体
    try:
        return ImageFont.load_default()
    except Exception:
        return None

def _render_hq(func):
    """Decorator: 高保真输出 — 固定 300 DPI 元数据，不降采样

    V4.0 重写：
      - 不再降采样（保留原始 v2 渲染器的完整分辨率）
      - 仅添加 300 DPI 元数据，确保 DOCX 打印质量
      - 确保输出为 PNG 格式
      - 保留原始色彩深度和透明度

    V4.1 修复：
      - 检测传入的 BytesIO 流是否已包含 300 DPI 元数据，如果已有则跳过重复编解码
      - 避免与 _image_to_stream 的双重 PNG 编码

    注意：v2 渲染器自身应使用 2.5x 缩放渲染以获得超采样抗锯齿效果，
    此装饰器仅负责最终输出质量标准化。
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if result is None:
            return None
        try:
            from PIL import Image as PilImage

            # BUG-MERMAID-IMG 修复：原代码 img = PilImage.open(result) 后
            # 没有显式 close()，PIL Image 持有底层内存/文件句柄，在高并发渲染
            # 场景下（后台任务批量生成配图）会累计造成资源耗尽。改用 with
            # 确保每次处理完自动释放。
            with PilImage.open(result) as img:
                # 检查是否已包含 300 DPI 元数据，避免重复编解码（V1.1 修复：使用 round 避免浮点精度问题）
                existing_dpi = img.info.get("dpi")
                if existing_dpi and len(existing_dpi) == 2:
                    dpi_x, dpi_y = existing_dpi
                    if round(dpi_x) == 300 and round(dpi_y) == 300:
                        result.seek(0)
                        return result
                buf = BytesIO()
                # 仅添加 300 DPI 元数据，不改变像素尺寸
                img.save(buf, format="PNG", optimize=True, dpi=(300, 300))
            buf.seek(0)
            return buf
        except Exception:
            result.seek(0)
            return result

    return wrapper

def _image_to_stream(image: Image.Image) -> BytesIO:
    """将 PIL Image 保存为 PNG 字节流（增强：300 DPI + 优化）"""
    buf = BytesIO()
    try:
        image.save(buf, format="PNG", optimize=True, dpi=(300, 300))
    except Exception:
        image.save(buf, format="PNG")
    buf.seek(0)
    return buf

def _text_width(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]

def _text_height(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]

def _draw_text_center(draw, box, text: str, font, fill: str, max_lines: int | None = None):
    x1, y1, x2, y2 = box
    lines = _wrap_text(draw, text, font, int(x2 - x1), max_lines=max_lines)
    line_h = max(16, _text_height(draw, "国", font) + 4)
    total_h = line_h * len(lines)
    y = y1 + max(0, (y2 - y1 - total_h) / 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        draw.text((x1 + (x2 - x1 - text_w) / 2, y), line, fill=fill, font=font)
        y += line_h

def _wrap_text(draw, text: str, font, max_width: int, max_lines: int | None = None) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return [""]
    lines = []
    current = ""
    for char in text:
        candidate = current + char
        if _text_width(draw, candidate, font) <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = char
        if max_lines and len(lines) >= max_lines:
            break
    if current and (not max_lines or len(lines) < max_lines):
        lines.append(current)
    if max_lines and len(lines) == max_lines and "".join(lines) != text:
        lines[-1] = _fit_text_with_ellipsis(draw, lines[-1], font, max_width)
    return lines[:max_lines] if max_lines else lines

def _fit_text_with_ellipsis(draw, text: str, font, max_width: int) -> str:
    value = text
    while value and _text_width(draw, f"{value}...", font) > max_width:
        value = value[:-1]
    return f"{value}..." if value else "..."

def _vertical_text_capacity(box_w: int, box_h: int, char_h: int,
                            char_gap: int = 4) -> tuple[int, int, int, int, int]:
    """竖排排版的行列几何 —— 返回 (每列字数, 可容纳列数, 总容量, 纵向步进, 横向步进)。

    刻意从 `_draw_text_vertical_center` 里抽出并**共用同一公式**：字号自适应需要
    预判「放大后还装不装得下最长标签」，两处口径一旦分叉，就会出现
    「自适应以为装得下、真正绘制仍被省略号收口」的假绿。
    """
    gap = max(0, int(char_gap))
    row_step = char_h + gap                  # 纵向步进：字高 + letter-spacing
    col_step = char_h + max(2, gap // 2)     # 横向步进：列间距（字宽方向）
    per_col = max(1, (box_h + gap) // row_step) if box_h > 0 else 1
    max_cols = max(1, box_w // col_step) if box_w > 0 else 1
    return per_col, max_cols, per_col * max_cols, row_step, col_step


def _draw_text_vertical_center(draw, box, text: str, font, fill: str, char_gap: int = 4) -> str:
    """在 box 内**竖排**居中绘制文字（自上而下成列、列自右向左，汉字保持直立）。

    对齐 CSS `writing-mode: vertical-rl` + `text-orientation: upright` 的观感
    （PIL 无原生竖排能力，故逐字绘制）：
      · 每列容量按 box 高度算出，超出即另起一列（自右向左，与竖排书写顺序一致）；
      · 列组整体在 box 内**水平居中**、每列文字块**垂直居中**
        （对应 flex 的 justify-content/align-items: center）；
      · 总字数超出「列容量 × 可容纳列数」时收敛成省略号 —— 宁可少画几个字，
        也绝不越界压到节点边框之外（旧横排版式里长标签同样以省略号收口）。

    返回**实际绘制出的文字**（未截断时即入参本身）：调用方据此判断有没有被
    省略号收口，字号自适应的端到端护栏也需要这个可观测出口。
    """
    x1, y1, x2, y2 = box
    text = str(text or "").strip()
    if not text:
        return text
    box_w = max(0, int(x2 - x1))
    box_h = max(0, int(y2 - y1))
    char_h = max(12, _text_height(draw, "国", font))
    gap = max(0, int(char_gap))
    per_col, _max_cols, capacity, row_step, col_step = _vertical_text_capacity(
        box_w, box_h, char_h, gap
    )
    if len(text) > capacity:
        text = f"{text[:max(1, capacity - 1)]}…"
    columns = [text[i:i + per_col] for i in range(0, len(text), per_col)]
    group_w = len(columns) * col_step
    right = x1 + (box_w + group_w) / 2       # 首列（最右列）的右边界
    for idx, column in enumerate(columns):
        center_x = right - col_step * idx - col_step / 2
        total_h = len(column) * row_step - gap
        y = y1 + max(0.0, (box_h - total_h) / 2)
        for ch in column:
            ch_w = _text_width(draw, ch, font)
            draw.text((center_x - ch_w / 2, y), ch, fill=fill, font=font)
            y += row_step
    return text


def _build_gantt_marks(duration: int, requested_tick: int, max_columns: int = 18) -> list[int]:
    duration = max(1, duration)
    requested_tick = max(1, requested_tick)
    max_columns = max(4, max_columns)
    nice_steps = [1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180]
    step = next(
        (v for v in nice_steps if v >= requested_tick and duration // v + 2 <= max_columns),
        requested_tick,
    )
    while duration // step + 2 > max_columns:
        step += requested_tick
    marks = list(range(0, duration + 1, step))
    if marks[-1] != duration:
        marks.append(duration)
    return marks


# ============================================================
# ✅ 跨渲染器「纸面可读性」单一事实源（2026-10-10 · R62）
# ============================================================
#   由来：R60 在架构图落地的闭式（`mermaid_architecture.paper_font_mm`）与 R61
#   「超宽自动拆图」**联合复测**时暴露出一条两边都没覆盖的边 —— 拆图把宽度降下来的
#   同时把高度堆上去了，而 DOCX 插图同时受「栏宽 16cm」「单图高 22cm」**两个**上限、
#   取更紧者等比缩小：一旦高上限反超宽度上限，整张拼接图（含**每一张**子图）会被
#   **再缩一次**，纸面字号不升反降（7 组超预算实测：4 组变差 −12% ~ −25%，且插入
#   宽度只剩 3.25~7.52cm，右半页空白）。
#   ⇒ 「加高换变窄」这类改造必须先过这一关，所以判语放在所有 PIL 渲染器的公共模块，
#     而不是再写进某个渲染器里。架构图的同名函数已委派到本层（不留两套口径）。
#   机理（R60 已用真实 PNG + 真实 `routers/export._fit_image_cm` 逐例核对，非推演）：
#     自然cm      = px × scale × 2.54 / DPI
#     fit         = min(1, col_cm / 自然宽cm, max_h_cm / 自然高cm)
#     纸面字号(mm) = 字号px × scale × 2.54 / DPI × fit × 10
#   scale 与 DPI 在宽度受限时分子分母同现、互相约掉 ⇒ 只剩「字号 ÷ 画布宽」一个比值；
#   但**高度受限时该约简不成立**，必须带上 canvas_h —— 这正是 R61 缺的那一半。
PAPER_SCALE_DEFAULT = 2.5       # 各渲染器统一 scale=2.5 超采样
PAPER_DPI_DEFAULT = 300.0       # `_image_to_stream` 写入的 PNG DPI
PAPER_COLUMN_WIDTH_CM = 16.0    # DOCX 正文栏宽上限（export._CHART_MAX_WIDTH_CM）
PAPER_MAX_HEIGHT_CM = 22.0      # 单图最大高度（export._CHART_MAX_HEIGHT_CM）
# ✅ R64：纸面字号**下限**的唯一读数落在这里。此前只有架构图自带一份 2.4mm
#    （R60），其余 PIL 渲染器对「印到纸上多大字」完全无感 —— 时间轴 24 事件
#    印到纸上只有 0.38mm（名称档）/ 0.28mm（最小档；R60 单档口径记的是
#    0.31mm，同一张图同一画布 15380x825，差别只在播报读的是**每一档**）
#    而日志一字未提。默认 2.4mm ≈ 6.8pt（中文「小六」2.3~
#    「六号」2.65mm），与 R60 架构图口径同值，两类图共用同一条线。
PAPER_FONT_MIN_MM = 2.4
# 读数顺序：通用名 CHART_PAPER_MIN_MM 优先，兼容既有的 ARCH_LEAF_PAPER_MIN_MM
#   （历史上只调过架构图那把旋钮；全局调档不该逼人改两个名字 ⇒ 两个都认）。
ENV_PAPER_FONT_MIN = ("CHART_PAPER_MIN_MM", "ARCH_LEAF_PAPER_MIN_MM")


def paper_font_mm(font_px: float, canvas_w: int, canvas_h: int = 0, title_h: int = 0,
                  *, scale: float = PAPER_SCALE_DEFAULT, dpi: float = PAPER_DPI_DEFAULT,
                  col_cm: float = PAPER_COLUMN_WIDTH_CM,
                  max_h_cm: float = PAPER_MAX_HEIGHT_CM) -> float:
    """DOCX 插入后图内文字的**纸面高度**（mm），宽/高**双上限**取更紧者。

    `canvas_h=0` → 只看宽度（等价 R60 之前的单上限口径）；`title_h` 是标题带高度，
    只在算总高时叠加。纯函数、绝不抛：非法尺寸/字号返回 0.0（调用方按「不达标」处理）。
    """
    if canvas_w <= 0 or font_px <= 0 or scale <= 0 or dpi <= 0:
        return 0.0
    per_px = scale * 2.54 / dpi * 10.0          # 1 图内像素 → 多少纸上毫米
    fit = 1.0
    w_cm = canvas_w * per_px / 10.0
    if w_cm > col_cm:
        fit = min(fit, col_cm / w_cm)
    if canvas_h > 0:
        h_cm = (canvas_h + title_h) * per_px / 10.0
        if h_cm > max_h_cm:
            fit = min(fit, max_h_cm / h_cm)
    return font_px * per_px * fit


def stitch_geometry(sizes, gap: int) -> tuple[int, int]:
    """竖向拼接（R61 拆图口径）后的整图外接尺寸：**宽取最大、高求和再加间隙**。

    `sizes` 为 `[(w, h), …]`；空输入返回 (0, 0)，调用方据此判「无可拼」。
    """
    pts = [(int(w), int(h)) for w, h in sizes]
    if not pts:
        return 0, 0
    return max(w for w, _ in pts), sum(h for _, h in pts) + int(gap) * (len(pts) - 1)


def paper_height_binds(canvas_w: int, canvas_h: int,
                       *, col_cm: float = PAPER_COLUMN_WIDTH_CM,
                       max_h_cm: float = PAPER_MAX_HEIGHT_CM) -> bool:
    """整图插入时是否由「高上限」决定缩放（即拆图换来的高度已反噬宽度收益）。

    整数交叉相乘（`h/w > max_h/col` ⇔ `h*col > w*max_h`），避免浮点边界抖动。
    """
    if canvas_w <= 0 or canvas_h <= 0:
        return False
    return canvas_h * col_cm > canvas_w * max_h_cm


def paper_floor_mm() -> float:
    """全仓统一的「纸面字号下限」（mm）。纯函数、绝不抛。

    空 / 非法 / ≤0 ⇒ 默认 2.4mm；显式**负数** ⇒ 0.0，语义「关闭告警」（毫米
    仍照常统计，只是不再 warning）—— 与架构图 `paper_font_min_mm()` 的语义
    **刻意不同**：那边「0 = 回到固定字号」是布局开关，这边只是播报开关。
    """
    for name in ENV_PAPER_FONT_MIN:
        v = os.environ.get(name, "")
        if not v.strip():
            continue
        try:
            f = float(v.strip())
        except ValueError:
            continue
        return f if f > 0 else 0.0
    return PAPER_FONT_MIN_MM


def font_px(font) -> int:
    """PIL 字体对象 → 像素字号；拿不到（默认位图字体 / 非字体对象）一律 0。绝不抛。

    播报口径直接取**真正用于绘制的那些字体对象**，而不是再抄一份字号常量表 ——
    抄表的话渲染器日后改字号，纸面播报会静默失真（R57「源码级契约静默失效」同类）。
    """
    try:
        return int(getattr(font, "size", 0) or 0)
    except (TypeError, ValueError):
        return 0


def paper_floor_note(label, fonts, canvas_w, canvas_h=0, *, log=None,
                     suggest="", primary=None):
    """把「这张图印到纸上多大字」写进日志：info 记**最差档**，warning 判**主文字档**。

    · `fonts`：本图真正用于绘制的 PIL 字体对象（口径来源见 `font_px`）；
    · `primary`：参与**达标判定**的主文字字体（架构图=节点/叶子档，时间轴=名称档）。
      省略时用 `fonts` 全集。**为什么要分两档**：日期/描述/序号等小字档在画布宽度
      地板下可能**数学上永远达不到**下限（时间轴 11px ÷ 960px → 1.83mm），按最差档
      告警会让每张图都红灯、告警失去信号价值；按主文字告警则「真的读不了」才响。
      观测口径（info）不打折，判定口径（warning）不滥用 —— 两条都在同一行日志里。
    · 空列表 / 非法尺寸 → 返回 0.0 且**不打日志**（宁缺毋滥）。
    · 本函数**只写日志**：不改像素、不改返回值、不改任何分支。
    """
    px = [font_px(f) for f in (fonts or [])]
    px = [v for v in px if v > 0]
    if not px or int(canvas_w or 0) <= 0:
        return 0.0
    lw = int(canvas_w)
    lh = int(canvas_h or 0)
    lg = log if log is not None else logger
    vals = [paper_font_mm(v, lw, lh) for v in px]
    vals = [v for v in vals if v > 0]
    if not vals:
        return 0.0
    worst = min(vals)
    judge = [font_px(f) for f in (primary if primary is not None else fonts or [])]
    judge = [paper_font_mm(v, lw, lh) for v in judge if v > 0]
    judge = [v for v in judge if v > 0]
    main = min(judge) if judge else worst
    floor = paper_floor_mm()
    try:
        lg.info("[纸面可读性] %s：画布 %dx%dpx，最差字号 %dpx → 纸面 %.2fmm，"
                "主文字 → %.2fmm（下限 %.2fmm）",
                label, lw, lh, px[vals.index(worst)], worst, main, floor)
        if 0 < floor and main < floor - 1e-6:
            lg.warning(
                "[纸面可读性] %s 主文字印到纸上只有 %.2fmm，低于下限 %.2fmm，"
                "建议减少条目或拆分图片%s",
                label, main, floor, suggest)
    except Exception:                                      # 播报失败绝不连累出图
        pass
    return round(worst, 2)


def delivered_worst_mm(fonts_px, sizes, gap: int, **kw) -> float:
    """拼接交付图里**最差**那张子图的纸面毫米数（双上限下的真实落纸效果）。

    缩放系数由**整图**外接尺寸决定（所有子图共享同一个 fit），而字号取各子图自己的
    —— 这正是「拆得越多字越小」能被量出来的原因。无子图时返回 0.0。
    """
    if not fonts_px or not sizes:
        return 0.0
    w_all, h_all = stitch_geometry(sizes, gap)
    vals = [paper_font_mm(f, w_all, h_all, **kw) for f in fonts_px]
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else 0.0
