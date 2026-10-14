"""解析提取模块 · R73 契约归一护栏（2026-10-12）

背景：`parse_file_content_ex` 的文档契约是「不可恢复的解析失败只抛
ParseError」，但各格式解析器会把底层库异常直接外泄 —— 实测 4 条逃逸路径：

  · .xls 含 OLE 头但结构损坏        → xlrd.compdoc.CompDocError
  · .xlsx 实为 .docx（污染 Content_Types）→ openpyxl KeyError
  · .xlsx sheet XML 损坏            → openpyxl OSError
  · .xls 实为 .xlsx                 → xlrd KeyError

逃逸违反契约：按契约只接 ParseError 的新调用方会冒成 500；两个既有路由靠
各自的泛化 except 兜底，用户只看到无差别的「文件无法识别或已损坏」。
本轮在格式分派层统一归一：库异常 → ParseError（可读文案），ParseError
原样透传（压缩炸弹/加密/缺依赖等既有语义与文案一字不改）。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import io
import re
import struct
import zipfile
from pathlib import Path

import pytest

from app.config import Settings
from app.routers import global_facts as gf
from app.routers import global_facts_docs as gfd
from app.services import file_parser as fp
from app.services import legacy_office as lo
from app.services.file_parser import ParseError


def _docx_like_zip() -> bytes:
    """一个结构合法但不是 xlsx 的 ZIP（把 .docx 改名成 .xlsx 的形态）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


def _corrupt_ole() -> bytes:
    """OLE 复合文档头合法、但内部结构损坏的 .xls（实测触发 CompDocError）。"""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(range(64))


# =========================================================================
# A：R73 契约归一 —— 逃逸路径必须收敛为 ParseError
# =========================================================================
class TestDispatchContractNormalization:
    def test_xls_corrupt_ole_becomes_parse_error(self):
        """OLE 头合法但结构损坏的 .xls → ParseError（此前是 CompDocError 逃逸）。"""
        xlrd = pytest.importorskip("xlrd")
        try:
            xlrd.compdoc.CompDocError  # noqa: B018 - 确保异常类型存在
        except AttributeError:
            pytest.skip("xlrd 版本无 compdoc.CompDocError")
        with pytest.raises(ParseError) as ei:
            fp.parse_file_content_ex(_corrupt_ole(), "a.xls")
        assert "XLS" in str(ei.value)

    def test_xlsx_docx_renamed_becomes_parse_error(self):
        """把 .docx 改名成 .xlsx → ParseError（此前是 openpyxl KeyError 逃逸）。"""
        pytest.importorskip("openpyxl")
        with pytest.raises(ParseError) as ei:
            fp.parse_file_content_ex(_docx_like_zip(), "a.xlsx")
        assert "XLSX" in str(ei.value)
        # 底层细节不得回传客户端（防泄露路径/内部符号）
        assert "Content_Types" not in str(ei.value)

    def test_user_message_has_no_internal_exception(self):
        """ParseError 文案必须是可操作的用户提示，不得透传库异常原文。"""
        xlrd = pytest.importorskip("xlrd")
        try:
            xlrd.compdoc.CompDocError  # noqa: B018
        except AttributeError:
            pytest.skip("xlrd 版本无 compdoc.CompDocError")
        with pytest.raises(ParseError) as ei:
            fp.parse_file_content_ex(_corrupt_ole(), "a.xls")
        msg = str(ei.value)
        assert "修补" not in msg and "marker" not in msg
        assert "重新上传" in msg

    def test_corrupt_xml_xlsx_becomes_parse_error(self):
        """sheet XML 损坏的 .xlsx → ParseError（此前是 openpyxl OSError 逃逸）。"""
        pytest.importorskip("openpyxl")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
            z.writestr("xl/workbook.xml", "<workbook>")
            z.writestr("xl/worksheets/sheet1.xml", "<worksheet>")
        with pytest.raises(ParseError):
            fp.parse_file_content_ex(buf.getvalue(), "a.xlsx")


# =========================================================================
# B：ParseError 原样透传 —— 既有语义与文案一字不改
# =========================================================================
class TestParseErrorPassthrough:
    def test_guard_archive_parseerror_preserved(self, monkeypatch):
        """压缩炸弹闸门的 ParseError 必须原样透传（不被包装成「文件已损坏」）。"""
        def _bomb(content, ftype):  # noqa: ANN001
            raise ParseError("归档条目过多，疑似压缩炸弹，已拒绝解析")
        monkeypatch.setattr(fp, "_guard_zip_archive", _bomb)
        with pytest.raises(ParseError) as ei:
            fp.parse_file_content_ex(b"PK\x03\x04" + b"\x00" * 32, "a.docx")
        assert "压缩炸弹" in str(ei.value)

    def test_ocr_parseerror_preserved(self, monkeypatch):
        """OCR 无引擎的 ParseError 必须原样透传（用户侧提示不变）。"""
        def _ocr(content, ftype, diag):  # noqa: ANN001
            raise ParseError("无可用 OCR 引擎，请安装 tesseract 或配置视觉模型")
        monkeypatch.setattr(fp, "_parse_image_ocr", _ocr)
        with pytest.raises(ParseError) as ei:
            fp.parse_file_content_ex(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "a.png")
        assert "OCR" in str(ei.value)


# =========================================================================
# C：防御纵深 —— 任何解析器意外异常都收敛为 ParseError
# =========================================================================
class TestDefenseInDepth:
    def test_unknown_runtime_error_becomes_parse_error(self, monkeypatch):
        """解析器抛任意 RuntimeError → ParseError（而非逃逸成 500）。"""
        def _boom(content):  # noqa: ANN001
            raise RuntimeError("模拟解析器内部故障")
        monkeypatch.setattr(fp, "_parse_docx", _boom)
        with pytest.raises(ParseError) as ei:
            fp.parse_file_content_ex(b"PK\x03\x04" + b"\x00" * 32, "a.docx")
        assert "DOCX" in str(ei.value)

    def test_valid_csv_still_parsed(self):
        """回归控制：合法文件不受影响。"""
        text, diag = fp.parse_file_content_ex(
            "设备名称,数量\n塔吊,2\n".encode("utf-8"), "a.csv")
        assert "设备名称" in text
        assert diag["file_type"] == "csv"

    def test_valid_txt_trailing_gb18030(self):
        """GB18030 文本仍正常（降级路径不受包装影响）。"""
        text, _diag = fp.parse_file_content_ex("中文内容".encode("gb18030"), "a.txt")
        assert "中文内容" in text


# =========================================================================
# D：静态接线锁 —— 分派包装块不得被后续重构静默摘除
# =========================================================================
class TestDispatchWrapperStaticGuard:
    def test_wrapper_present_in_dispatch(self):
        """分派区必须存在 try/except：except ParseError 原样 raise + 泛化归一。"""
        src = inspect.getsource(fp.parse_file_content_ex)
        assert "except ParseError:" in src, "ParseError 透传分支被摘除"
        assert "重新上传" in src, "归一化 ParseError 文案被摘除"
        # 归一分支必须覆盖整个分派区（出现在 _parse_excel / _parse_image_ocr
        # 调用之后的位置），防止包装只罩住前半段
        assert src.index("except Exception") > src.index("_parse_image_ocr"), (
            "泛化归一 except 必须在全部格式分派之后")


# =========================================================================
# E：R73 续作护栏 —— G2 死占位删除 / G3 文档路径漂移 / G4 OLE 预检
# =========================================================================
_CFB_HEADER_SIZE = 512
#: 构造最小 CFB 时占用的 8 字节魔数 —— 复用 file_parser 的单一事实源，
#: 不另写十六进制字面量（转义字面量容易被编辑工具展平成坏字节，R56 同族坑）
_CFB_SIG = fp.OLE_COMPOUND_HEADERS[0]


def _cfb_header(*, n_fat=1, n_dir=1, n_mini=0, n_difat=0,
                first_dir=1, minor=0x003E, major=4,
                byte_order=0xFFFE, sec_shift=9, mini_shift=6,
                extra_bytes=1024) -> bytes:
    """构造「结构自洽的最小 CFB」，单字段可覆写。

    默认参数下扇区算术恰好成立（2 个 512B 扇区承载 1 FAT + 1 目录），
    反向用例逐字段破坏它来验证预检能识别。
    """
    h = bytearray(_CFB_HEADER_SIZE)
    h[0:8] = _CFB_SIG
    struct.pack_into("<HHHHH", h, 0x18,
                     minor, major, byte_order, sec_shift, mini_shift)
    struct.pack_into("<III", h, 0x28, n_dir, n_fat, first_dir)
    struct.pack_into("<I", h, 0x40, n_mini)
    struct.pack_into("<I", h, 0x48, n_difat)
    return bytes(h) + b"\x00" * extra_bytes


class TestQuotaPlaceholderRemoved:
    """G2：上传配额的静态占位删除，权威值只剩 global_facts 一处。"""

    _QUOTA = ("MAX_UPLOAD_BYTES", "MAX_UPLOAD_FILES_PER_REQUEST",
              "MAX_UPLOAD_TOTAL_BYTES")

    def test_docs_module_has_no_local_quota_constant(self):
        """不得再出现配额的**字面量副本**（本仓「同一判据两份取值」同族反例）。"""
        assigned = set()
        for node in ast.parse(inspect.getsource(gfd)).body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        assigned.add(t.id)
        assert not (assigned & set(self._QUOTA)), "global_facts_docs 又出现配额副本"

    def test_dead_gf_const_helper_gone(self):
        """`_gf_const` 从未被调用（配额读点直接走 `gf.*`），留着就是死代码。"""
        funcs = {n.name for n in ast.parse(inspect.getsource(gfd)).body
                 if isinstance(n, ast.FunctionDef)}
        assert "_gf_const" not in funcs

    def test_quota_authority_still_in_global_facts(self):
        assert gf.MAX_UPLOAD_BYTES == 30 * 1024 * 1024
        assert gf.MAX_UPLOAD_FILES_PER_REQUEST == 20
        assert gf.MAX_UPLOAD_TOTAL_BYTES == 200 * 1024 * 1024
        # MAX_PARSED_CHARS 是 R70 收敛的落库上限单一事实源，删占位不得误伤它
        assert gfd.MAX_PARSED_CHARS == gf.MAX_PARSED_CHARS

    def test_upload_still_reads_quota_dynamically(self):
        """删占位后上传路径仍动态读 `gf.*`（保留 monkeypatch 覆盖配额的能力）。"""
        src = inspect.getsource(gfd.upload_documents)
        for name in self._QUOTA:
            assert f"gf.{name}" in src, f"上传路径未动态读取 {name}"


class TestDocstringTestPathDrift:
    """G3：测试 docstring 引用的前端测试文件必须真实存在（防路径漂移假绿）。"""

    def test_r72_reference_points_to_a_real_file(self):
        mod = importlib.import_module("test_import_parse_closeout_r72_20261012")
        refs = re.findall(r"``([A-Za-z0-9_.\-/]+\.(?:test\.[jt]sx?))``",
                          mod.__doc__ or "")
        assert refs, "R72 docstring 不再引用前端测试文件（G3 护栏失去锚点）"
        tests_dir = Path(__file__).resolve().parents[2] / "frontend" / "src" / "tests"
        for ref in refs:
            assert (tests_dir / ref).is_file(), (
                f"docstring 引用的前端测试文件不存在：{ref}")


class TestOleHeaderPreflight:
    """G4：OLE 头预检只判定「合法 CFB 的必要条件」，绝不误拒正常的老版 Word。"""

    def test_valid_minimal_cfb_accepted(self):
        assert lo.validate_ole_compound_header(_cfb_header()) is None

    def test_non_cfb_container_is_out_of_scope(self):
        """不是 CFB 容器（ZIP / 短文本）明确不在职责内 → 保持旧行为交后端判定。"""
        assert lo.validate_ole_compound_header(b"PK\x03\x04" + b"\x00" * 4000) is None
        assert lo.validate_ole_compound_header(b"abc") is None

    @pytest.mark.parametrize("kwargs", [
        {"minor": 0x003F}, {"major": 99}, {"byte_order": 0xFEFF},
        {"sec_shift": 6}, {"sec_shift": 13}, {"mini_shift": 7},
    ], ids=lambda d: f"{next(iter(d))}={next(iter(d.values()))}")
    def test_header_field_out_of_spec_rejected(self, kwargs):
        assert lo.validate_ole_compound_header(_cfb_header(**kwargs)), kwargs

    def test_truncated_file_detected_by_sector_arithmetic(self):
        reason = lo.validate_ole_compound_header(_cfb_header(extra_bytes=0))
        assert "截断" in reason

    def test_v4_requires_directory_sector(self):
        reason = lo.validate_ole_compound_header(_cfb_header(major=4, n_dir=0))
        assert "目录扇区数为 0" in reason

    def test_zero_fat_sectors_rejected(self):
        assert "FAT" in lo.validate_ole_compound_header(_cfb_header(n_fat=0))

    def test_v3_excel_shape_accepted(self):
        """真实 Excel 97-2003（CFB v3）形态必须放行：目录扇区数 0、FAT 内联。

        字段布局取自一份 1.23MB 的真实 .xls（v3 · 512B 扇区 · 19 个 FAT 扇区 ·
        1 个迷你 FAT 扇区 · 共 2408 扇区）—— 早期版本因扇区计数偏移读错，
        把这份正常文件误判成「已截断」；这条用例钉住该回归。
        """
        h = _cfb_header(major=3, n_dir=0, n_fat=19, n_mini=1, n_difat=0,
                        first_dir=1, extra_bytes=2408 * 512 - 512)
        assert lo.validate_ole_compound_header(h) is None

    @pytest.mark.parametrize("kwargs", [
        {"first_dir": 0xFFFFFFFE}, {"first_dir": 0xFFFFFFFF},
        {"first_dir": 99},
    ], ids=["保留值ENDOFCHAIN", "保留值FATSECT", "目录索引越界"])
    def test_reserved_or_out_of_range_sector_rejected(self, kwargs):
        assert lo.validate_ole_compound_header(_cfb_header(**kwargs)), kwargs


class TestOlePreflightWiring:
    """G4 接线：早退必须先于后端探测；开关关闭时完全回到旧行为。"""

    def test_convert_short_circuits_before_backend_probe(self, monkeypatch):
        probed = {"n": 0}

        def _probe(ext="doc"):
            probed["n"] += 1
            return []

        monkeypatch.setattr(lo, "detect_office_backends", _probe)
        with pytest.raises(ParseError) as ei:
            lo.convert_legacy_word_to_docx(
                _cfb_header(first_dir=0xFFFFFFFE), "broken.doc")
        assert "已损坏" in str(ei.value)
        assert probed["n"] == 0, "预检早退后不应再探测/调用转换后端"

    def test_switch_off_fully_restores_old_behavior(self, monkeypatch):
        """关掉预检 = 完全不早退（走原后端链路，报原「缺组件」文案）。"""
        monkeypatch.setattr(lo, "legacy_ole_preflight_enabled", lambda: False)
        monkeypatch.setattr(lo, "detect_office_backends", lambda ext="doc": [])
        with pytest.raises(ParseError) as ei:
            lo.convert_legacy_word_to_docx(
                _cfb_header(first_dir=0xFFFFFFFE), "b.doc")
        assert str(ei.value) == lo.LEGACY_OFFICE_REQUIRED_MESSAGE

    def test_preflight_reason_fail_open_on_own_exception(self, monkeypatch):
        """预检自身异常必须 fail-open，不得阻断原本可解析的老版 Word。"""
        monkeypatch.setattr(
            lo, "validate_ole_compound_header",
            lambda _c: (_ for _ in ()).throw(RuntimeError("模拟预检故障")))
        assert lo.ole_preflight_reason(_cfb_header(first_dir=0xFFFFFFFE)) is None

    def test_switch_off_skips_validation_entirely(self, monkeypatch):
        monkeypatch.setattr(lo, "legacy_ole_preflight_enabled", lambda: False)
        monkeypatch.setattr(
            lo, "validate_ole_compound_header",
            lambda _c: (_ for _ in ()).throw(
                AssertionError("开关关闭时不得执行判定")))
        assert lo.ole_preflight_reason(_cfb_header(first_dir=0xFFFFFFFE)) is None

    def test_config_default_on_and_flag_readable(self):
        assert Settings.model_fields["legacy_ole_preflight"].default is True
        assert lo.legacy_ole_preflight_enabled() is True

    def test_wiring_order_static_lock(self):
        """预检调用必须位于 `detect_office_backends` 之前，否则仍白等后端。"""
        src = inspect.getsource(lo.convert_legacy_word_to_docx)
        assert "ole_preflight_reason(content)" in src
        assert src.index("ole_preflight_reason(content)") < src.index(
            "detect_office_backends("), "预检必须早于后端探测"


# =========================================================================
# E（续）：G5 收敛后 —— global_facts 侧的「非正数回落」兜底须独立可测
# =========================================================================
# R73 续作 G5 把端点的兜底读取器（_positive_setting + _UPLOAD_LIMIT_DEFAULTS）
# 删除后，配额的「非法配置回落安全默认」语义全部由 global_facts._positive_int_setting
# 在模块加载时承担。该函数此前只被端点间接覆盖（端点自带副本时），现在端点
# 不再有副本，回落逻辑必须独立可测 —— 否则 ``UPLOAD_MAX_BYTES=0`` 的误配会
# 静默把上限变成 0（等于关闭守卫），却没有任何测试会红。
class TestGlobalFactsQuotaFallback:
    """``global_facts._positive_int_setting`` 的非正数/非整数回落（G5 收敛后的兜底出口）。"""

    def test_zero_falls_back_to_default(self, monkeypatch):
        from app.routers import global_facts as gf
        monkeypatch.setattr(gf.settings, "upload_max_bytes", 0)
        assert gf._positive_int_setting("upload_max_bytes", 30 * 1024 * 1024) == 30 * 1024 * 1024

    def test_negative_falls_back_to_default(self, monkeypatch):
        from app.routers import global_facts as gf
        monkeypatch.setattr(gf.settings, "upload_max_bytes", -5)
        assert gf._positive_int_setting("upload_max_bytes", 30 * 1024 * 1024) == 30 * 1024 * 1024

    def test_non_numeric_falls_back_to_default(self, monkeypatch):
        from app.routers import global_facts as gf
        monkeypatch.setattr(gf.settings, "upload_max_bytes", "abc")
        assert gf._positive_int_setting("upload_max_bytes", 30 * 1024 * 1024) == 30 * 1024 * 1024

    def test_none_falls_back_to_default(self, monkeypatch):
        from app.routers import global_facts as gf
        monkeypatch.setattr(gf.settings, "upload_max_bytes", None)
        assert gf._positive_int_setting("upload_max_bytes", 30 * 1024 * 1024) == 30 * 1024 * 1024

    def test_positive_value_passes_through(self, monkeypatch):
        from app.routers import global_facts as gf
        monkeypatch.setattr(gf.settings, "upload_max_bytes", 50 * 1024 * 1024)
        assert gf._positive_int_setting("upload_max_bytes", 30 * 1024 * 1024) == 50 * 1024 * 1024

    def test_missing_attr_falls_back_to_default(self):
        """settings 没有该字段时回落默认（防御：未来字段改名不致崩溃）。"""
        from app.routers import global_facts as gf
        # 用一个 settings 上不存在的字段名，getattr 的第三参兜底
        assert gf._positive_int_setting("upload_max_bytes_does_not_exist", 30 * 1024 * 1024) == 30 * 1024 * 1024



# =========================================================================
# E（续）：G4 设计红线硬化 —— 预检只读头部，绝不解析内容流
# =========================================================================
class TestOlePreflightHeaderOnly:
    """G4 设计红线：``validate_ole_compound_header`` **只判定 CFB 头部必要条件**，
    绝不做内容语义解析（解析内容流就有误拒正常文件的风险，违背 §12.3 红线）。

    这条护栏锁住「递延项 G4-②（内容语义级损坏）刻意不做」的边界 ——
    将来若有人为了「检出 Workbook 流损坏」往本函数里加 ``olefile``/``open``/
    流式读取，本护栏会立即红，迫使新增能力另立函数、不污染本预检。
    """

    _FORBIDDEN = ("olefile", "ZipFile", "compound_file", "CompoundFile",
                  ".read(", "open(", "io.BytesIO")

    def test_validate_function_body_is_header_only(self):
        import inspect
        src = inspect.getsource(lo.validate_ole_compound_header)
        for token in self._FORBIDDEN:
            assert token not in src, (
                f"validate_ole_compound_header 不得引用 {token!r}（违反「只读头部、"
                f"不解析内容流」红线 —— 内容级损坏判定应另立函数，不污染本预检）")

    def test_validate_function_only_uses_struct_unpack_from(self):
        """头部字段读取只能经 ``struct.unpack_from``（固定偏移读字节），不得有文件/流 I/O。

        这是 ``test_validate_function_body_is_header_only`` 的结构化补强：
        字符串扫描能抓 ``olefile``/``open``，但抓不住将来某次「用 ``mmap`` 或
        ``memoryview`` 切片做语义解析」的引入。本例遍历 AST 调用节点，断言全部
        调用要么是 ``struct.unpack_from``，要么是纯内置（``len``/``bytes``/
        ``isinstance``/``range``）—— 任何流式/文件 I/O 调用都会红。
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(lo.validate_ole_compound_header))
        allowed = {"unpack_from", "len", "bytes", "isinstance", "range"}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            c = n.func
            if isinstance(c, ast.Attribute):
                name = c.attr
            elif isinstance(c, ast.Name):
                name = c.id
            else:
                continue
            assert name in allowed, (
                f"validate_ole_compound_header 出现非头部读取调用 {name!r}"
                f"（仅允许 struct.unpack_from + 纯内置；内容流解析应另立函数）")


# =========================================================================
# E（续）：G4 递延项硬化 —— item ①（非 CFB/ZIP 容器）+ item ④（真实 .doc 语料）
# =========================================================================
# 这两条护栏锁住「刻意递延」的架构依据，使其可审计、防未来会话重新审视或
# 「修复」一个不存在的问题。
class TestZipContainerNoPreflightNeeded:
    """G4 递延项 ① 硬化：非 CFB / ZIP 容器**不需要**预检 —— 它们不走 LibreOffice。

    调查结论（2026-10-12）：``LEGACY_WORD_EXTENSIONS = {"doc", "wps"}`` 全是
    OLE/CFB 容器；ZIP 容器（``.docx`` / ``.xlsx``）走 ``file_parser._parse_docx``
    的 Python ``zipfile`` 快路径，**不经过 LibreOffice 转换**，损坏 ZIP 由
    ``zipfile.BadZipFile`` 毫秒级抛出（并经 R73 G1 契约归一收敛为 ParseError）。
    因此「损坏 ZIP 白等 120s」的症状**结构上不存在**，G4 对非 CFB 返回 ``None``
    交后端判定是正确行为，**不需要新增 ZIP 容器预检**。

    本护栏锁三条不变量，防未来有人把 ``.docx`` 误塞进 ``LEGACY_WORD_EXTENSIONS``
    （那才会真正引入「损坏 ZIP 白等」症状），或拆掉 ``_parse_docx`` 的
    ``BadZipFile`` 兜底（那会让损坏 ZIP 逃逸成 500）。
    """

    _ZIP_EXTS = {"docx", "xlsx", "zip", "xlsm", "docm"}

    def test_legacy_word_extensions_are_cfb_only(self):
        """只有 OLE/CFB 容器扩展名走 LibreOffice 转换。"""
        assert lo.LEGACY_WORD_EXTENSIONS, "LEGACY_WORD_EXTENSIONS 不得为空"
        for ext in lo.LEGACY_WORD_EXTENSIONS:
            assert ext not in self._ZIP_EXTS, (
                f"扩展名 {ext!r} 是 ZIP 容器却进了 LEGACY_WORD_EXTENSIONS —— "
                f"ZIP 容器走 zipfile 快路径，不该经 LibreOffice（会引入「损坏 ZIP "
                f"白等 120s」症状，违背 G4 递延项 ① 的架构依据）")

    def test_docx_parser_catches_bad_zipfile(self):
        """损坏 ZIP 作为 .docx → ``ParseError``（毫秒级快失败，不白等、不逃逸成 500）。

        这是 G4 递延项 ①「ZIP 不需要预检」的**行为级依据**：``_parse_docx`` 走
        Python ``zipfile`` 快路径，损坏 ZIP 由 ``zipfile`` 即时抛错，经 R73 G1
        契约归一收敛为 ``ParseError``。本例用三种损坏形态（坏本地头 / 非ZIP /
        截断）验证全部快失败 —— 证明「损坏 ZIP 白等 120s」的症状**结构上不存在**。
        """
        from app.services import file_parser as fp
        samples = [
            b"PK\x03\x04" + b"\x00" * 40,        # 坏本地文件头
            b"PK" + b"\x00" * 40,                 # 根本不是 ZIP
            b"PK\x03\x04\x14\x00" + b"\x00" * 30,  # 截断
        ]
        for data in samples:
            with pytest.raises(ParseError):
                fp.parse_file_content_ex(data, "a.docx")

    def test_zip_container_returns_none_from_ole_preflight(self):
        """G4 预检对 ZIP 容器返回 None（交后端 zipfile 快路径），不误拒。"""
        # ZIP 本地文件头魔数 PK\x03\x04
        assert lo.validate_ole_compound_header(b"PK\x03\x04" + b"\x00" * 4000) is None


class TestHeaderCheckIsFormatAgnostic:
    """G4 递延项 ④ 硬化：头结构预检**格式无关**，不读流名 —— v3 Excel 形态
    测试已结构性地覆盖 v3 Word ``.doc``。

    ``validate_ole_compound_header`` 只看 CFB **容器头部**字段（小版本 / 大版本 /
    字节序 / 扇区位移 / 扇区算术），**不读任何流名**（Workbook / WordDocument /
    0Table / Data 等）。CFB 容器层 ``.doc`` 与 ``.xls`` 的唯一差异是内部的流名，
    头部字段布局完全相同 —— 因此 ``test_v3_excel_shape_accepted``（用真实 1.23MB
    Excel 97 ``.xls`` 字段布局钉死）**结构性地覆盖**了 v3 Word ``.doc`` 的头形态。

    缺真实 ``.doc`` 语料时，**不存在**「头级判据对 .doc 漏检」的风险 ——
    头级判据根本不看流名。将来若收到误拒反馈，应**补真实 .doc 语料而非放宽判据**
    （§12.7-4 已记录）。本护栏锁住「不读流名」这一性质，防有人为了「区分 .doc /
    .xls」往头预检里加流名读取（那会破坏格式无关性，且违背「只判必要条件」红线）。
    """

    _STREAM_NAMES = ("Workbook", "WordDocument", "Book", "0Table", "1Table",
                     "Data", "SummaryInformation", "DocumentSummaryInformation")

    def test_validate_does_not_read_stream_names(self):
        """头预检源码不得引用任何 CFB 流名（格式无关性的硬锁）。"""
        import inspect
        src = inspect.getsource(lo.validate_ole_compound_header)
        for name in self._STREAM_NAMES:
            assert name not in src, (
                f"validate_ole_compound_header 引用了流名 {name!r} —— 头预检必须是"
                f"格式无关的（不读流名），区分 .doc/.xls 是内容语义层职责，不在 G4")

    def test_v3_excel_shape_structurally_covers_v3_doc(self):
        """v3 Excel 形态护栏（test_v3_excel_shape_accepted）用的字段布局对 v3 .doc 同样成立。

        此例用同一布局但**显式标注**「这是 v3 CFB 容器形态，.doc 与 .xls 在头级
        无差异」—— 让「为何不需要单独的 v3 .doc 形态测试」可审计。
        """
        h = _cfb_header(major=3, n_dir=0, n_fat=19, n_mini=1, n_difat=0,
                        first_dir=1, extra_bytes=2408 * 512 - 512)
        # 同一份头布局对 .doc（WordDocument 流）与 .xls（Workbook 流）都合法 ——
        # 头预检不看流名，故 test_v3_excel_shape_accepted 已覆盖 v3 .doc 头形态。
        assert lo.validate_ole_compound_header(h) is None

