"""OCR 能力模块 + 文件解析错误语义 单元测试

覆盖：
- ocr_capabilities / ocr_capabilities_async（能力探测字段与类型）
- _engine_order（OCR_ENGINE 配置对引擎顺序的影响）
- _extract_rapidocr_text（兼容 RapidOCR 多种返回结构）
- ocr_bytes_sync（空数据 / 禁用 / 无引擎时的可操作报错）
- file_parser.ParseError（图片无 OCR 引擎、.xls 缺依赖时抛异常而非返回错误字符串）
"""
import pytest
from app.services import ocr as ocr_mod
from app.services.file_parser import ParseError, parse_file_content

# ============================================================
# 能力探测
# ============================================================

class TestOcrCapabilities:
    def test_sync_capabilities_keys(self):
        cap = ocr_mod.ocr_capabilities()
        for key in ("enabled", "engine_pref", "lang", "tesseract", "rapidocr", "vision"):
            assert key in cap
        assert isinstance(cap["tesseract"], bool)
        assert isinstance(cap["rapidocr"], bool)

    async def test_async_capabilities(self, db_conn):
        cap = await ocr_mod.ocr_capabilities_async()
        assert "available" in cap
        assert isinstance(cap["vision"], bool)
        assert isinstance(cap["available"], bool)

    async def test_vision_unavailable_without_config(self, db_conn):
        # 空 ai_config → 无视觉模型
        assert await ocr_mod.vision_available(force=True) is False


# ============================================================
# 引擎顺序
# ============================================================

class TestEngineOrder:
    def test_auto_default(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ocr_engine", "auto")
        assert ocr_mod._engine_order() == ["tesseract", "rapidocr", "vision"]

    def test_prefer_rapidocr(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ocr_engine", "rapidocr")
        order = ocr_mod._engine_order()
        assert order[0] == "rapidocr"

    def test_prefer_vision(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ocr_engine", "vision")
        assert ocr_mod._engine_order()[0] == "vision"


# ============================================================
# RapidOCR 结果解析（多版本兼容）
# ============================================================

class TestRapidOcrParsing:
    def test_single_line_shape(self):
        # (result, elapse)：result 为单行 [box, text, score]
        out = ([[[0, 0], [1, 0], [1, 1], [0, 1]], "你好", 0.99], 0.1)
        assert ocr_mod._extract_rapidocr_text(out) == "你好"

    def test_multi_line_shape(self):
        # (result, elapse)：result 为 list[[box, text, score], ...]
        out = ([[None, "第一行", 0.9], [None, "第二行", 0.8]], 0.1)
        assert ocr_mod._extract_rapidocr_text(out) == "第一行\n第二行"

    def test_plain_list(self):
        out = [[None, "A", 0.9], [None, "B", 0.8]]
        assert ocr_mod._extract_rapidocr_text(out) == "A\nB"

    def test_object_with_txts(self):
        class _Out:
            txts = ("A", "B")

        assert ocr_mod._extract_rapidocr_text(_Out()) == "A\nB"

    def test_none_result(self):
        assert ocr_mod._extract_rapidocr_text((None, 0.1)) == ""

    def test_empty(self):
        assert ocr_mod._extract_rapidocr_text(None) == ""


# ============================================================
# ocr_bytes_sync 行为
# ============================================================

class TestOcrBytesSync:
    def test_empty_data(self):
        res = ocr_mod.ocr_bytes_sync(b"")
        assert res.text == "" and res.engine == "none"

    def test_disabled_by_switch(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ocr_enabled", False)
        with pytest.raises(ocr_mod.OcrUnavailableError):
            ocr_mod.ocr_bytes_sync(b"abc")

    def test_engine_off(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ocr_enabled", True)
        monkeypatch.setattr(settings, "ocr_engine", "off")
        with pytest.raises(ocr_mod.OcrUnavailableError):
            ocr_mod.ocr_bytes_sync(b"abc")

    def test_no_engine_gives_actionable_hint(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ocr_enabled", True)
        monkeypatch.setattr(settings, "ocr_engine", "auto")
        monkeypatch.setattr(ocr_mod, "tesseract_available", lambda: False)
        monkeypatch.setattr(ocr_mod, "rapidocr_available", lambda: False)
        monkeypatch.setattr(ocr_mod, "_vision_available_sync", lambda: False)
        with pytest.raises(ocr_mod.OcrUnavailableError) as ei:
            ocr_mod.ocr_bytes_sync(b"\x89PNG fake")
        msg = str(ei.value)
        assert "OCR" in msg and "pip install" in msg


# ============================================================
# file_parser 错误语义（不得返回错误字符串）
# ============================================================

class TestParseErrorSemantics:
    def _no_engine(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ocr_enabled", True)
        monkeypatch.setattr(settings, "ocr_engine", "auto")
        monkeypatch.setattr(ocr_mod, "tesseract_available", lambda: False)
        monkeypatch.setattr(ocr_mod, "rapidocr_available", lambda: False)
        monkeypatch.setattr(ocr_mod, "_vision_available_sync", lambda: False)

    def test_image_without_ocr_raises(self, monkeypatch):
        """没有 OCR 引擎时应抛 ParseError，而不是把错误说明当作正文返回。"""
        self._no_engine(monkeypatch)
        with pytest.raises(ParseError) as ei:
            parse_file_content(b"\x89PNG fake bytes", "scan.png")
        assert "OCR" in str(ei.value)

    def test_xls_without_xlrd_raises(self):
        try:
            import xlrd  # noqa: F401
            pytest.skip("xlrd 已安装，该分支不适用")
        except ImportError:
            pass
        with pytest.raises(ParseError) as ei:
            parse_file_content(b"not a real xls", "old.xls")
        assert "xls" in str(ei.value).lower()

    def test_txt_still_works(self):
        assert parse_file_content("正文内容".encode("utf-8"), "a.txt") == "正文内容"
