"""OCR 能力模块（多层引擎，按可用性自动降级）

背景：扫描件 / 图片型 PDF 必须依赖 OCR 才能提取文字，而旧实现只支持
本机 tesseract（系统级二进制）。一旦用户机器未安装 tesseract，上传的
扫描件就完全无法解析 —— 「图片类资料不可提取」。

本模块提供三条互相独立的识别通道，任一条可用即可工作：

  1. tesseract    —— 本地，需系统安装 tesseract 及 chi_sim 语言包
  2. RapidOCR     —— 本地，纯 pip 安装（rapidocr-onnxruntime），无需系统二进制
                     （推荐：Windows 用户零配置即可用中文识别）
  3. 视觉大模型    —— 云端，复用「AI 配置」中已填写的视觉模型（*-vl / *-vision /
                     gpt-4o / gemini / claude 等），无需任何本地依赖

对外 API：
  - ocr_capabilities()        —— 同步能力探测（诊断/日志用）
  - ocr_capabilities_async()  —— 异步能力探测（含视觉模型可用性）
  - ocr_bytes_sync(data)      —— 同步识别（本地引擎优先，必要时桥接视觉模型）
  - ocr_bytes_vision(data)    —— 异步识别（仅视觉模型）

设计原则：
  - 任何引擎都不可用时抛出 OcrUnavailableError，并在消息中给出「如何启用」，
    绝不返回「OCR 解析失败：...」这类字符串 —— 后者会被上层误当作正文入库。
  - 可选依赖全部惰性导入，缺库不影响服务启动。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("ocr")

# 视觉能力探测缓存（避免每次 OCR 都查库）
_VISION_CACHE: dict = {"value": None, "ts": 0.0}
_VISION_TTL = 300.0
_VISION_LOCK = threading.Lock()

# RapidOCR 引擎实例缓存（构造耗时，复用）
_RAPID_ENGINE = None
_RAPID_LOCK = threading.Lock()

# ✅ 修复（2026-09-17）：RapidOCR 的 ONNXRuntime 会话并非保证线程安全。并发上传时，
# file_parser 通过 asyncio.to_thread 在不同工作线程里共享同一个 _RAPID_ENGINE 并发推理，
# 可能崩溃或输出串扰（乱码）。这里用进程级锁串行化本地引擎（RapidOCR/Tesseract）推理，
# 避免并发调用共享引擎实例；视觉大模型走独立连接池，无需串行。
_OCR_CALL_LOCK = threading.Lock()


class OcrUnavailableError(RuntimeError):
    """没有任何可用的 OCR 引擎（调用方应把消息原样展示给用户）。"""


@dataclass
class OcrResult:
    text: str
    engine: str

    def __bool__(self) -> bool:
        return bool(self.text and self.text.strip())


# ---------------------------------------------------------------------------
# 引擎可用性探测
# ---------------------------------------------------------------------------

def _tesseract_binary() -> str:
    """返回 tesseract 可执行路径（未安装返回空串）。"""
    try:
        from app.config import settings
    except Exception:
        settings = None
    if settings is not None and getattr(settings, "tesseract_path", ""):
        return settings.tesseract_path
    return shutil.which("tesseract") or ""


def tesseract_available() -> bool:
    if not _tesseract_binary():
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def rapidocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _get_rapid_engine():
    """惰性构造 RapidOCR 引擎（线程安全，复用实例）。"""
    global _RAPID_ENGINE
    if _RAPID_ENGINE is not None:
        return _RAPID_ENGINE
    with _RAPID_LOCK:
        if _RAPID_ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR
            _RAPID_ENGINE = RapidOCR()
    return _RAPID_ENGINE


async def vision_available(force: bool = False) -> bool:
    """当前是否配置了「支持视觉」的 AI Provider。结果带 TTL 缓存。"""
    now = time.time()
    if not force and _VISION_CACHE["value"] is not None \
            and now - _VISION_CACHE["ts"] < _VISION_TTL:
        return bool(_VISION_CACHE["value"])
    try:
        from app.services.ai.provider_factory import get_vision_providers
        value = bool(await get_vision_providers())
    except Exception as e:
        # ✅ P1（B4 · 2026-10-05）：探测异常是**瞬时故障**（DB/配置读抖动），与
        #    「确实没配视觉模型」是两回事。旧实现把 False 一并写进 TTL 缓存，
        #    一次抖动就会让扫描件/图片型资料**连续 5 分钟**静默跳过 OCR ——
        #    解析结果直接缺页且没有任何告警。故异常路径不推进缓存：
        #    有旧结论就沿用旧结论，否则本次降级为 False、下次重试。
        logger.debug("视觉模型可用性探测失败（不缓存失败结论）: %s", e)
        if _VISION_CACHE["value"] is not None:
            return bool(_VISION_CACHE["value"])
        return False
    _VISION_CACHE["value"] = value
    _VISION_CACHE["ts"] = now
    return value


def _engine_order() -> list[str]:
    """按配置与可用性给出引擎尝试顺序。"""
    from app.config import settings
    prefer = (getattr(settings, "ocr_engine", "auto") or "auto").strip().lower()
    local = ["tesseract", "rapidocr"]
    if prefer in ("tesseract", "rapidocr"):
        local = [prefer] + [e for e in local if e != prefer]
    order = local + ["vision"]
    if prefer == "vision":
        order = ["vision"] + local
    return order


def _unavailable_hint() -> str:
    return (
        "未检测到可用的 OCR 引擎，扫描件/图片型资料无法识别。三选一即可启用：\n"
        "① 安装内置离线引擎（推荐，无需系统软件）：在 backend 目录执行 "
        "`pip install -r requirements-ocr.txt` 后重启后端；\n"
        "② 安装 Tesseract-OCR（需勾选中文语言包 chi_sim），并在 .env 配置 "
        "TESSERACT_PATH；\n"
        "③ 在「文本模型配置」中添加一个视觉模型（型号含 vl / vision / gpt-4o / gemini / claude）。"
    )


# ---------------------------------------------------------------------------
# 图像预处理（提升识别率）
# ---------------------------------------------------------------------------

def _preprocess(data: bytes):
    """转灰度 + 小图放大 + 自动对比度，返回 PIL.Image（缺 PIL 时返回 None）。"""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        import io as _io
        img = Image.open(_io.BytesIO(data))
        img = img.convert("L")  # 灰度：OCR 引擎对灰度更稳
        w, h = img.size
        if max(w, h) < 1200:  # 小图放大可显著提升小字号识别率
            scale = min(2400 / max(w, h, 1), 3.0)
            if scale > 1.05:
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img = ImageOps.autocontrast(img)
        return img
    except Exception as e:
        logger.debug("OCR 预处理失败（回退原图）: %s", e)
        return None


# ---------------------------------------------------------------------------
# 单引擎实现
# ---------------------------------------------------------------------------

def _ocr_tesseract(data: bytes, lang: str) -> str:
    import pytesseract

    from app.config import settings
    if getattr(settings, "tesseract_path", ""):
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_path
    cfg = getattr(settings, "tesseract_data_path", "")
    config = f'--tessdata-dir "{cfg}"' if cfg else ""
    img = _preprocess(data)
    if img is not None:
        with _OCR_CALL_LOCK:
            return pytesseract.image_to_string(img, lang=lang, config=config) or ""
    import io as _io

    from PIL import Image
    with _OCR_CALL_LOCK:
        return pytesseract.image_to_string(Image.open(_io.BytesIO(data)),
                                           lang=lang, config=config) or ""


def _ocr_rapidocr(data: bytes) -> str:
    engine = _get_rapid_engine()
    img = _preprocess(data)
    target = data
    if img is not None:
        try:
            import numpy as np
            target = np.array(img)
        except ImportError:
            target = data
    with _OCR_CALL_LOCK:
        out = engine(target)
    return _extract_rapidocr_text(out)


def _looks_like_single_line(x) -> bool:
    """判断 x 是否形如单行结果 [box, text, score]。"""
    return (
        isinstance(x, (list, tuple)) and len(x) == 3
        and isinstance(x[1], str)
        and isinstance(x[2], (int, float))
    )


def _extract_rapidocr_text(out) -> str:
    """兼容 RapidOCR 不同版本的返回结构。

    可能形态：
      - (result, elapse)：result 为 list[[box, text, score], ...] 或 None
      - 对象含 .txts 属性（新版 rapidocr）
      - 单行 [box, text, score]（未包在外层列表里）
    """
    result = out
    if isinstance(out, tuple) and len(out) == 2:
        result = out[0]              # (result, elapse)
    elif hasattr(out, "txts"):
        texts = getattr(out, "txts", None)
        if texts is not None:
            try:
                return "\n".join(str(t) for t in texts if t)
            except TypeError:
                return ""

    if result is None:
        return ""

    # 规范化：若传入的本身就是单行，则包一层，统一按"行列表"处理
    rows = result
    if _looks_like_single_line(result):
        rows = [result]

    lines: list[str] = []
    if isinstance(rows, (list, tuple)):
        for item in rows:
            if isinstance(item, str):
                if item:
                    lines.append(item)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                txt = item[1]
                if txt:
                    lines.append(str(txt))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 视觉大模型 OCR
# ---------------------------------------------------------------------------

_VISION_PROMPT = (
    "你是 OCR 引擎。请逐行、完整地提取这张图片中的全部文字，保持原始阅读顺序与"
    "版式结构（表格用 | 分隔行）。只输出识别到的文字本身，不要翻译、不要总结、"
    "不要添加任何解释或说明。若图片中没有文字，输出空。"
)


async def ocr_bytes_vision(data: bytes, prompt: str = "") -> str:
    """用已配置的视觉大模型识别图片（OpenAI Vision 多模态格式）。"""
    from app.services.ai.provider_factory import get_vision_providers
    providers = await get_vision_providers()
    if not providers:
        raise OcrUnavailableError(_unavailable_hint())

    png = data
    img = _preprocess(data)
    if img is not None:
        try:
            import io as _io
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            png = buf.getvalue()
        except Exception:
            png = data
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    from app.services.ai.providers.base import BaseProvider
    messages = [BaseProvider.build_vision_message(prompt or _VISION_PROMPT, [data_url])]
    last_err: Exception | None = None
    for provider in providers:
        try:
            text = await provider.chat(messages, temperature=0.0)
            if text and text.strip():
                return text.strip()
        except Exception as e:  # 逐个降级尝试
            last_err = e
            logger.warning("视觉 OCR 调用失败（%s）: %s", getattr(provider, "model", "?"), e)
    if last_err is not None:
        raise RuntimeError(f"视觉模型 OCR 失败：{last_err}")
    return ""


# ---------------------------------------------------------------------------
# 同步桥接（file_parser 为同步函数）
# ---------------------------------------------------------------------------

def _run_coro_sync(coro):
    """在同步上下文中执行协程：无线程外事件循环时直接 asyncio.run；
    若当前线程已有运行中的循环，则放到独立线程执行，避免重入错误。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _vision_available_sync() -> bool:
    try:
        return bool(_run_coro_sync(vision_available()))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 对外统一入口
# ---------------------------------------------------------------------------

def ocr_bytes_sync(data: bytes, *, lang: str = "") -> OcrResult:
    """同步 OCR：按引擎顺序尝试，全部失败抛出 OcrUnavailableError。"""
    from app.config import settings
    if not getattr(settings, "ocr_enabled", True) or \
            (getattr(settings, "ocr_engine", "auto") or "").lower() == "off":
        raise OcrUnavailableError(
            "OCR 已被配置禁用（OCR_ENABLED=false 或 OCR_ENGINE=off）")

    if not data:
        return OcrResult("", "none")

    lang = lang or getattr(settings, "ocr_lang", "chi_sim+eng") or "chi_sim+eng"
    errors: list[str] = []
    for name in _engine_order():
        try:
            if name == "tesseract":
                if not tesseract_available():
                    continue
                text = _ocr_tesseract(data, lang)
            elif name == "rapidocr":
                if not rapidocr_available():
                    continue
                text = _ocr_rapidocr(data)
            elif name == "vision":
                if not _vision_available_sync():
                    continue
                text = _run_coro_sync(ocr_bytes_vision(data))
            else:
                continue
            if text and text.strip():
                logger.info("OCR 成功（引擎=%s，%d 字）", name, len(text))
                return OcrResult(text.strip(), name)
            errors.append(f"{name}: 未识别到文字")
        except OcrUnavailableError:
            raise
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.warning("OCR 引擎 %s 失败: %s", name, e)

    if errors and all("未识别到文字" not in e for e in errors):
        # 有引擎但都报错 → 给出具体原因，便于排查
        raise OcrUnavailableError("OCR 识别失败：" + "；".join(errors[:3]))
    if any("未识别到文字" in e for e in errors):
        return OcrResult("", "none")
    raise OcrUnavailableError(_unavailable_hint())


def ocr_capabilities() -> dict:
    """同步能力报告（不含视觉模型探测，用于日志/同步场景）。"""
    from app.config import settings
    return {
        "enabled": bool(getattr(settings, "ocr_enabled", True)),
        "engine_pref": getattr(settings, "ocr_engine", "auto"),
        "lang": getattr(settings, "ocr_lang", "chi_sim+eng"),
        "tesseract": tesseract_available(),
        "rapidocr": rapidocr_available(),
        "vision": None,  # 需异步探测
    }


async def ocr_capabilities_async() -> dict:
    """异步能力报告（含视觉模型探测），供诊断接口使用。"""
    cap = ocr_capabilities()
    cap["vision"] = await vision_available(force=True)
    cap["available"] = bool(cap["tesseract"] or cap["rapidocr"] or cap["vision"])
    if not cap["available"]:
        cap["hint"] = _unavailable_hint()
    return cap
