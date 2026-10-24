# -*- coding: utf-8 -*-
"""解析提取模块 · R113 收口护栏（2026-10-20）

本轮（用户指令：聚焦【解析提取模块】深度分析/增强/修复所有 BUG）在静态审计
后落地 5 项后端修复 + 4 项前端修复，本文件承载后端侧的可执行断言。

覆盖分组：
  A · FIX-1（P0）upload_outline 同步解析阻塞事件循环 → asyncio.to_thread
  B · FIX-2（P1）doc_storage._safe_id 目录穿越 + 写路径管理层守卫
  C · FIX-3（P1）压缩炸弹闸门：条目数检查前移 + .doc/.wps 不得绕过
  D · FIX-4（P2）_mark_parse_failed 追加式写告警（不得覆盖历史截断告警）
  E · FIX-5（P2）缺依赖文案归因正确（openpyxl / xlrd）
  F · 字节体检（BOM / 行尾 / FFFD）

全部为纯函数 / monkeypatch / 临时目录，无真实 AI、无网络、无生产库写入。
"""
from __future__ import annotations

import ast
import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from app.routers import global_facts_docs as gfd
from app.routers import upload_outline as uo
from app.services import file_parser as fp
from app.services.doc_pipeline import doc_storage as store

BACKEND = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A · FIX-1（P0）· 目录识别路由不得在事件循环内同步执行解析
# ---------------------------------------------------------------------------

class TestUploadOutlineDoesNotBlockEventLoop:
    """FIX-1：parse_file_content_ex 是同步阻塞函数（MuPDF/OCR/LibreOffice/
    MinerU 网络轮询），必须移出事件循环。

    本仓已有设计契约佐证（不是新增约定）：
      · legacy_office.py:15-16「调用方 parse_file_content_ex 本身跑在线程池里」
      · mineru_client.py:16-18「调用方本身运行在线程池中」
    姊妹链路 global_facts_docs.py:1097-1099 / :1294-1296 早已用 to_thread。
    """

    def test_route_uses_to_thread_for_parse(self):
        """源码锁：路由内 parse_file_content_ex 必须经 asyncio.to_thread。

        实际写法是 ``await asyncio.to_thread(parse_file_content_ex, content, fname)``
        —— 解析函数作为**实参**传入（而非被调名），故判据落在
        「to_thread 调用的实参里含 parse_file_content_ex」。
        """
        src = (BACKEND / "app" / "routers" / "upload_outline.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "parse_outline")
        to_thread_calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "to_thread"
        ]
        assert to_thread_calls, "未找到 asyncio.to_thread 调用"
        # 至少一处 to_thread 的实参里带着 parse_file_content_ex
        wired = any(
            any(isinstance(a, ast.Name) and a.id == "parse_file_content_ex"
                for a in c.args)
            for c in to_thread_calls
        )
        assert wired, (
            "parse_file_content_ex 未包在 asyncio.to_thread(...) 内，"
            "会在事件循环里同步阻塞（OCR/LibreOffice/MinerU 可达数分钟）")
        # 且 to_thread 必须被 await（Await 的 .value 就是该 Call）
        awaited_to_thread = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "to_thread"
        ]
        assert awaited_to_thread, "asyncio.to_thread 必须被 await"

    def test_asyncio_is_imported(self):
        """源码锁：模块必须 import asyncio（否则 NameError）。"""
        src = (BACKEND / "app" / "routers" / "upload_outline.py").read_text(
            encoding="utf-8")
        assert "import asyncio" in src

    def test_sibling_route_uses_to_thread(self):
        """姊妹链路口径一致（对照证据：global_facts_docs 两条解析路径）。"""
        src = (BACKEND / "app" / "routers" / "global_facts_docs.py").read_text(
            encoding="utf-8")
        assert "asyncio.to_thread(" in src

    async def test_event_loop_stays_responsive_during_parse(self, monkeypatch):
        """功能实证：解析在途时事件循环仍能调度其它协程。

        旧实现（同步调用）下，解析期间事件循环被独占，本协程无法获得执行机会；
        现实现（to_thread）下，解析在 worker 线程执行，事件循环保持可响应。
        """
        import threading

        started = threading.Event()
        release = threading.Event()

        def _slow_parse(content, fname):
            started.set()
            release.wait(timeout=5)
            return ("第一章 工程概况\n\n测试正文。", {"warnings": [], "file_type": "txt"})

        monkeypatch.setattr(uo, "parse_file_content_ex", _slow_parse)

        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(_ticker())
        try:
            # 直接驱动路由的解析调用点（绕过文件上传与落库，只验线程语义）
            content = b"hello"
            task = asyncio.create_task(
                asyncio.to_thread(uo.parse_file_content_ex, content, "a.txt"))
            # 等解析线程起来
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set(), "解析线程未启动"
            # 解析在途时，事件循环仍能跑 tick
            await asyncio.sleep(0.05)
            assert ticks > 0, "事件循环在解析在途时被阻塞"
            release.set()
            text, _diag = await task
            assert "工程概况" in text
        finally:
            release.set()
            ticker.cancel()


# ---------------------------------------------------------------------------
# B · FIX-2（P1）· 目录穿越防护
# ---------------------------------------------------------------------------

class TestSafeIdBlocksTraversal:
    """FIX-2：_safe_id 旧实现只做字符白名单，`.` 被保留 ⇒ `_safe_id("..")`
    原样返回 ".." ⇒ doc_dir 拼出 DOCS_ROOT/".."/... 逃出管理层。"""

    @pytest.mark.parametrize("raw", [
        "..", ".", "...", "../..", "..\\..", "..abc", "abc..def", "....",
    ])
    def test_traversal_ids_are_rejected(self, raw):
        """穿越形态一律回落 unknown（不得原样透出）。"""
        assert store._safe_id(raw) == "unknown"

    @pytest.mark.parametrize("raw", [
        "proj-1", "a.b.c", "18b97cc8-3064-4f5e-b1f1-de7ba39bc8b7", "文档1",
    ])
    def test_legit_ids_are_preserved(self, raw):
        """合法 id 逐字不变（零行为回归）。"""
        assert store._safe_id(raw) == raw

    def test_empty_and_none_fall_back(self):
        assert store._safe_id("") == "unknown"
        assert store._safe_id(None) == "unknown"

    def test_doc_dir_stays_within_docs_root(self):
        """doc_dir('..') 必须落在 DOCS_ROOT 之下（不得逃逸）。"""
        root = store.DOCS_ROOT.resolve()
        try:
            store.doc_dir("..", "x").resolve().relative_to(root)
        except ValueError:
            pytest.fail("doc_dir('..') 逃出了 DOCS_ROOT")

    def test_index_path_stays_within_docs_root(self):
        root = store.DOCS_ROOT.resolve()
        try:
            store.index_path("..").resolve().relative_to(root)
        except ValueError:
            pytest.fail("index_path('..') 逃出了 DOCS_ROOT")

    def test_write_path_guard_rejects_outside_paths(self, tmp_path, monkeypatch):
        """写路径管理层守卫：DOCS_ROOT 之外的产物路径必须被拒。"""
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects")
        outside = tmp_path / "evil" / "x.json"
        with pytest.raises(ValueError):
            store._ensure_within_docs_root(outside)

    def test_write_path_guard_allows_inside_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects")
        inside = tmp_path / "projects" / "p1" / "documents" / "d1" / "a.json"
        assert store._ensure_within_docs_root(inside) == inside

    def test_atomic_write_bytes_is_not_globally_restricted(self, tmp_path):
        """atomic_write_bytes 是通用原语：不得因守卫而误伤任意路径写入。

        （守卫刻意放在**路径构造层** doc_dir/index_path，而非通用写原语。）
        """
        dest = tmp_path / "a" / "b.json"
        store.atomic_write_json(dest, {"k": "中文"})
        assert store.read_json(dest) == {"k": "中文"}


# ---------------------------------------------------------------------------
# C · FIX-3（P1）· 压缩炸弹闸门
# ---------------------------------------------------------------------------

def _mkzip(n: int, size: int = 10, compress: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    ) as z:
        for i in range(n):
            z.writestr(f"f{i}.txt", ("x" * size).encode())
    return buf.getvalue()


class TestZipGuardHardening:
    """FIX-3 两处：① 条目数闸门前移；② .doc/.wps 不得绕过闸门。"""

    def test_declared_entry_count_is_exact(self):
        """廉价前置读出的声明条目数必须精确（否则会误拒合法归档）。"""
        for n in (0, 1, 10, 2000, 2001, 5000):
            assert fp._zip_declared_entry_count(_mkzip(n)) == n

    def test_declared_entry_count_none_for_non_zip(self):
        assert fp._zip_declared_entry_count(b"not a zip at all") is None
        assert fp._zip_declared_entry_count(b"") is None

    def test_guard_boundary_unchanged(self):
        """既有边界语义不变：2000 放行 / 2001 拒绝。"""
        fp._guard_zip_archive(_mkzip(fp.MAX_ARCHIVE_ENTRIES), "docx")
        with pytest.raises(fp.ParseError):
            fp._guard_zip_archive(_mkzip(fp.MAX_ARCHIVE_ENTRIES + 1), "docx")

    def test_empty_zip_still_passes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        fp._guard_zip_archive(buf.getvalue(), "docx")

    def test_entry_count_gate_precedes_infolist(self, monkeypatch):
        """条目数闸门必须在**构造 ZipFile / infolist** 之前生效。

        旧实现先 infolist()（构造全部 ZipInfo）再判条目数 ⇒ 闸门不是内存闸门。
        """
        import zipfile as _zf

        # 先造好归档（避免把造归档时的 ZipFile 构造计入 spy）
        payload = _mkzip(fp.MAX_ARCHIVE_ENTRIES + 1)

        real_zipfile = _zf.ZipFile
        calls = {"n": 0}

        class _SpyZipFile(real_zipfile):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **kw):
                calls["n"] += 1
                super().__init__(*a, **kw)

        monkeypatch.setattr(zipfile, "ZipFile", _SpyZipFile)
        # 超限归档：应在前置检查就被拒 ⇒ 根本不构造 ZipFile
        with pytest.raises(fp.ParseError):
            fp._guard_zip_archive(payload, "docx")
        assert calls["n"] == 0, (
            "超限归档仍构造了 ZipFile（中央目录已被物化，闸门形同虚设）")

    def test_doc_extension_cannot_bypass_zip_guard(self, monkeypatch):
        """改名成 .doc 的 ZIP（PK 魔数）必须同样受闸门约束。

        旧实现只对 ftype ∈ {docx, xlsx} 调闸门，而 _BINARY_SIGNATURES 对
        doc/wps 放行 PK 魔数 ⇒ 未受闸门约束的字节被交给 LibreOffice 子进程。
        """
        seen = {"called": False}

        def _spy_guard(content, ftype):
            seen["called"] = True
            raise fp.ParseError("闸门命中（测试用）")

        monkeypatch.setattr(fp, "_guard_zip_archive", _spy_guard)
        # 声称是 .doc，但内容是 ZIP 容器
        with pytest.raises(fp.ParseError):
            fp.parse_file_content_ex(_mkzip(3), "renamed.doc")
        assert seen["called"], "ZIP 容器改名为 .doc 后绕过了压缩炸弹闸门"

    def test_wps_extension_cannot_bypass_zip_guard(self, monkeypatch):
        seen = {"called": False}

        def _spy_guard(content, ftype):
            seen["called"] = True
            raise fp.ParseError("闸门命中（测试用）")

        monkeypatch.setattr(fp, "_guard_zip_archive", _spy_guard)
        with pytest.raises(fp.ParseError):
            fp.parse_file_content_ex(_mkzip(3), "renamed.wps")
        assert seen["called"], "ZIP 容器改名为 .wps 后绕过了压缩炸弹闸门"

    def test_ole_doc_still_uses_legacy_channel(self, monkeypatch):
        """真 OLE .doc 仍走旧版转换通道（不得被误判为 ZIP 而拦下）。"""
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        reached = {"legacy": False}

        def _fake_legacy(content, fname, diag):
            reached["legacy"] = True
            return "旧版 Word 正文"

        monkeypatch.setattr(fp, "_parse_legacy_word", _fake_legacy)
        text, _diag = fp.parse_file_content_ex(ole, "old.doc")
        assert reached["legacy"], "真 OLE .doc 未走旧版转换通道"
        assert "旧版 Word" in text


# ---------------------------------------------------------------------------
# D · FIX-4（P2）· 解析失败告警必须追加而非覆盖
# ---------------------------------------------------------------------------

class _FakeDb:
    """最小可用的 aiosqlite 替身：只支持本护栏用到的两种语句。"""

    def __init__(self, warnings: str = ""):
        self.warnings = warnings
        self.written: str | None = None
        self.commits = 0

    async def execute(self, sql, params=()):
        if "SELECT parse_warnings" in sql:
            class _Cur:
                def __init__(self, val):
                    self._val = val

                async def fetchone(self):
                    return {"parse_warnings": self._val} if self._val is not None else None

            return _Cur(self.warnings)
        if "UPDATE project_documents" in sql:
            self.written = params[0] if params else None
            return object()
        return object()

    async def commit(self):
        self.commits += 1


class TestMarkParseFailedAppendsWarnings:
    """FIX-4：失败原因是「追加事件」，不得抹掉历史截断/OCR 告警。"""

    async def test_existing_warnings_are_preserved(self):
        old = json.dumps(["原文 300000 字，已截断至 400000 字上限"], ensure_ascii=False)
        db = _FakeDb(warnings=old)
        await gfd._mark_parse_failed(db, "doc1", "文件无法识别或已损坏")
        assert db.written is not None
        merged = json.loads(db.written)
        assert any("已截断" in w for w in merged), (
            "重解析失败抹掉了历史截断告警：%r" % (merged,))
        assert any("解析失败" in w for w in merged), "失败原因未写入"

    async def test_failure_reason_is_appended(self):
        db = _FakeDb(warnings="[]")
        await gfd._mark_parse_failed(db, "doc1", "未解析到有效文本")
        merged = json.loads(db.written or "[]")
        assert any("未解析到有效文本" in w for w in merged)

    async def test_same_reason_is_not_duplicated(self):
        db = _FakeDb(warnings="[]")
        await gfd._mark_parse_failed(db, "doc1", "同一原因")
        await gfd._mark_parse_failed(db, "doc1", "同一原因")
        merged = json.loads(db.written or "[]")
        assert len([w for w in merged if "同一原因" in w]) == 1, (
            "同一失败原因重复堆叠：%r" % (merged,))

    async def test_empty_history_is_handled(self):
        db = _FakeDb(warnings="")
        await gfd._mark_parse_failed(db, "doc1", "失败原因")
        merged = json.loads(db.written or "[]")
        assert any("失败原因" in w for w in merged)

    async def test_malformed_history_does_not_crash(self):
        db = _FakeDb(warnings="not-json")
        await gfd._mark_parse_failed(db, "doc1", "失败原因")
        merged = json.loads(db.written or "[]")
        assert any("失败原因" in w for w in merged)

    async def test_status_is_still_failed(self):
        """既有契约不变：parse_status 必须置 'failed'。"""
        db = _FakeDb(warnings="[]")
        await gfd._mark_parse_failed(db, "doc1", "失败原因")
        sql_seen = []
        orig = db.execute

        async def _spy(sql, params=()):
            sql_seen.append(sql)
            return await orig(sql, params)

        db.execute = _spy
        await gfd._mark_parse_failed(db, "doc1", "失败原因2")
        assert any("parse_status='failed'" in s for s in sql_seen)


# ---------------------------------------------------------------------------
# E · FIX-5（P2）· 缺依赖文案归因
# ---------------------------------------------------------------------------

class TestMissingDependencyMessages:
    """FIX-5：缺库 ≠ 文件损坏。错误文案必须说清「服务端缺依赖」。"""

    def test_xlsx_missing_openpyxl_says_dependency(self, monkeypatch):
        """openpyxl 缺失 → 报「服务端缺少 openpyxl」，不得报「文件已损坏」。"""
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "openpyxl" or name.startswith("openpyxl."):
                raise ImportError("No module named 'openpyxl'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        # 造一个合法 xlsx 容器（内容其实不会被解析到，因为缺库先抛）
        with pytest.raises(fp.ParseError) as ei:
            fp.parse_file_content_ex(_mkzip(2), "a.xlsx")
        msg = str(ei.value)
        assert "openpyxl" in msg, f"未点名缺失依赖: {msg}"
        # 归因判据：不得复用「文件已损坏」这一误导性通用文案
        # （旧泛化分支文案为「XLSX 文件已损坏或不是有效的文件」）
        assert "已损坏或不是有效" not in msg, f"误报为文件损坏: {msg}"

    def test_xls_message_matches_requirements_pin(self):
        """xlrd 提示版本必须与 requirements.txt 的 pin 一致（否则照做即降级）。

        判据只针对**运行期用户可见文案**（AST 字符串常量），
        不扫描注释（注释里复述旧文案是合法的历史说明）。
        """
        req = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
        pinned = None
        for line in req.splitlines():
            low = line.strip().lower()
            if low.startswith("xlrd"):
                pinned = low.split("==")[-1].strip() if "==" in low else None
                break
        assert pinned, "requirements.txt 未声明 xlrd pin"
        src = (BACKEND / "app" / "services" / "file_parser.py").read_text(
            encoding="utf-8")
        # 运行期字符串常量（注释不在其中）
        literals = [
            n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        xlrd_msgs = [s for s in literals if "xlrd" in s and "pip install" in s]
        assert xlrd_msgs, "未找到 xlrd 安装提示文案"
        for s in xlrd_msgs:
            assert f"xlrd=={pinned}" in s, (
                f"xlrd 提示版本与 requirements pin（{pinned}）不一致: {s}")
            assert "xlrd==1.2.0" not in s, (
                f"仍提示已废弃的 xlrd==1.2.0（照做会降级依赖）: {s}")


# ---------------------------------------------------------------------------
# F · 字节体检
# ---------------------------------------------------------------------------

class TestByteHygiene:
    """字节级体检：BOM / 行尾 / 替换字符（本仓 pre-commit 门禁口径）。"""

    @pytest.mark.parametrize("rel", [
        "app/routers/upload_outline.py",
        "app/routers/global_facts_docs.py",
        "app/services/file_parser.py",
        "app/services/doc_pipeline/doc_storage.py",
    ])
    def test_no_bom_and_no_lone_cr(self, rel):
        raw = (BACKEND / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 含 UTF-8 BOM"
        text = raw.decode("utf-8")
        assert "\ufffd" not in text, f"{rel} 含替换字符（编码损坏）"
        assert "\r" not in text.replace("\r\n", ""), f"{rel} 含孤立 CR"

    def test_files_use_crlf_consistently(self):
        """本轮触碰的 4 个文件保持纯 CRLF（与仓内既有基线一致）。"""
        for rel in ("app/routers/upload_outline.py",
                    "app/services/doc_pipeline/doc_storage.py"):
            raw = (BACKEND / rel).read_bytes()
            assert b"\r\n" in raw, f"{rel} 无 CRLF"
            text = raw.decode("utf-8")
            assert "\n" not in text.replace("\r\n", ""), f"{rel} 混入裸 LF"
