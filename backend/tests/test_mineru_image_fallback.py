"""图片 OCR 的 MinerU 云端兜底回归测试（2026-09-22）。

引入背景（对齐 OpenBidKit `fileService` 的解析降级链）：
参考实现里 MinerU 云端解析支持 png/jpg/jp2/webp/gif/bmp 等图片格式，
而本软件此前只给 **PDF** 做 MinerU 兜底 —— 扫描件以图片形式上传
（图纸附件、照片型招标文件）时，本地没有 OCR 引擎就直接失败，即使用户
已经配好了 MinerU 也用不上。

本文件覆盖：
1. 未配置 MINERU_PROVIDER → 无本地引擎时仍抛 ParseError（旧行为不变）；
2. 配置了 MINERU_PROVIDER → 本地无引擎时走云端兜底，并写入解析诊断；
3. 本地 OCR 有有效结果 → **不调用**云端（不该多花钱）；
4. 本地 OCR 跑通但几乎无文字 → 走云端兜底；
5. 本地引擎异常（非「不可用」）→ 同样走云端兜底；
6. 云端返回空内容 → 不冒充成功（回落原错误/空结果）。
"""
import pytest

import app.services.ocr as ocr_mod
from app.services.file_parser import ParseError, parse_file_content_ex
import app.services.mineru_client as mineru_client

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _configure_mineru(monkeypatch, provider: str = "agent") -> None:
    monkeypatch.setattr(mineru_client.settings, "mineru_enabled", True)
    monkeypatch.setattr(mineru_client.settings, "mineru_provider", provider)


def _no_local_engine(monkeypatch) -> None:
    def _boom(data, **kwargs):
        raise ocr_mod.OcrUnavailableError("未检测到可用的 OCR 引擎")

    monkeypatch.setattr(ocr_mod, "ocr_bytes_sync", _boom)


# ---------------------------------------------------------------------------
# 1. 未配置云端 → 行为与旧版一致
# ---------------------------------------------------------------------------

def test_without_mineru_config_keeps_old_error(monkeypatch):
    monkeypatch.setattr(mineru_client.settings, "mineru_provider", "")
    _no_local_engine(monkeypatch)

    with pytest.raises(ParseError) as exc:
        parse_file_content_ex(_PNG, "扫描件.png")
    assert "OCR" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. 配置云端 → 兜底成功
# ---------------------------------------------------------------------------

def test_mineru_fallback_used_when_local_engine_missing(monkeypatch):
    _configure_mineru(monkeypatch)
    _no_local_engine(monkeypatch)
    monkeypatch.setattr(mineru_client, "parse_with_mineru",
                        lambda content, name: "# 扫描件内容\n这是云端识别结果")

    text, diag = parse_file_content_ex(_PNG, "扫描件.png")

    assert "云端识别结果" in text
    assert any("MinerU" in w for w in diag["warnings"]), (
        "云端兜底必须写入解析诊断（用户要能知道内容是怎么来的）")


def test_mineru_fallback_used_when_local_engine_raises(monkeypatch):
    """非 OcrUnavailableError 的引擎异常（依赖缺失/图像损坏）也走云端。"""
    _configure_mineru(monkeypatch)

    def _boom(data, **kwargs):
        raise RuntimeError("引擎内部错误")

    monkeypatch.setattr(ocr_mod, "ocr_bytes_sync", _boom)
    monkeypatch.setattr(mineru_client, "parse_with_mineru",
                        lambda content, name: "云端兜底成功内容")

    text, _diag = parse_file_content_ex(_PNG, "扫描件.png")
    assert "云端兜底成功内容" in text


# ---------------------------------------------------------------------------
# 3. 本地成功 → 不调用云端
# ---------------------------------------------------------------------------

def test_local_ocr_success_does_not_call_cloud(monkeypatch):
    _configure_mineru(monkeypatch)
    calls: list[str] = []

    monkeypatch.setattr(
        ocr_mod, "ocr_bytes_sync",
        lambda data, **kwargs: ocr_mod.OcrResult(text="本地识别出的足够长的文本", engine="tesseract"))

    def _should_not_be_called(content, name):  # pragma: no cover
        calls.append(name)
        return "云端结果"

    monkeypatch.setattr(mineru_client, "parse_with_mineru", _should_not_be_called)

    text, _diag = parse_file_content_ex(_PNG, "扫描件.png")
    assert "本地识别" in text
    assert not calls, "本地已有有效结果时不得调用云端（避免无谓花费）"


# ---------------------------------------------------------------------------
# 4 / 6. 本地结果过少 / 云端为空
# ---------------------------------------------------------------------------

def test_local_empty_text_triggers_cloud(monkeypatch):
    _configure_mineru(monkeypatch)
    monkeypatch.setattr(
        ocr_mod, "ocr_bytes_sync",
        lambda data, **kwargs: ocr_mod.OcrResult(text="  ", engine="tesseract"))
    monkeypatch.setattr(mineru_client, "parse_with_mineru",
                        lambda content, name: "云端补齐的图纸文字")

    text, _diag = parse_file_content_ex(_PNG, "扫描件.jpg")
    assert "云端补齐的图纸文字" in text


def test_cloud_empty_result_does_not_fake_success(monkeypatch):
    _configure_mineru(monkeypatch)
    _no_local_engine(monkeypatch)
    monkeypatch.setattr(mineru_client, "parse_with_mineru",
                        lambda content, name: "   \n  ")

    with pytest.raises(ParseError) as exc:
        parse_file_content_ex(_PNG, "扫描件.png")
    # 仍应为「无可用 OCR 引擎」语义，而不是伪装成解析成功
    assert "OCR" in str(exc.value)


def test_cloud_exception_recorded_as_warning(monkeypatch):
    """云端失败不应中断流程：无本地引擎时按原错误抛出，仅记录诊断。"""
    _configure_mineru(monkeypatch)
    _no_local_engine(monkeypatch)

    def _fail(content, name):
        raise ParseError("MinerU 云端解析轮询超时")

    monkeypatch.setattr(mineru_client, "parse_with_mineru", _fail)

    with pytest.raises(ParseError):
        parse_file_content_ex(_PNG, "扫描件.png")
