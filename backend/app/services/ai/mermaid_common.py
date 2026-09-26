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
    from PIL import Image, ImageDraw, ImageFont
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
    import functools

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

