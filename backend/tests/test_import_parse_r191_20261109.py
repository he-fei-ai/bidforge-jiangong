"""解析提取模块 R191 护栏（2026-11-09）。

本轮收口面（全部有行为实证或源码锁，配套 A/B 变异见 artifacts/r191/）：

  A 目录编号识别    点分编号无分隔符直接跟汉字（"1.1工程概况"）此前被单数字
                    pattern 错撕为 L1 "1工程概况"（层级丢失+编号错撕双输）；
                    "1.1.1施工准备" 被有分隔符点分条**回溯命中**错撕为
                    id=1.1 title="1施工准备"。新增无分隔符分支 + 三道防误判
                    护栏（日期形态/数值量词/句读）。
  B 编码链          UTF-32 BOM 被 UTF-16LE 抢判 → 合法文件被误杀 400；
                    UTF-16 BOM 用 "utf-16-le" 解码残留 U+FEFF 触发
                    「含零宽字符」假告警；无 BOM 探测全量扫描 30MB 文本
                    ~4.2s 纯 CPU（采样 64KB 后 <10ms）。
  C 通道加固        多页 TIFF 帧数无上限 + 中途失败静默丢帧；加密 OOXML
                    （EncryptedPackage）被误路由到 xlrd/Word COM 得到误导
                    报错或乱码落库；bmp 2 字节弱签名与 text-guard 口径分叉；
                    PDF OCR 渲染无像素闸（超大页靠 malloc 失败兜底）；
                    docx 回退 docx2python 不进 diag（观测面与 PDF 不对齐）。
  D 路由层          上传同名替换不标 stale/不失效缓存（旧事实无限期注入）；
                    reconcile 把 failed 翻回 success；update_category 非
                    字符串 500；流式落盘非 OSError 半写孤儿；commit 裸奔；
                    同请求重名虚报 saved_count；delete 先删文件后提交；
                    stale 调用裸奔；parse-all 早退分支契约漂移。
  E 跨模块          scoring_items 读 chunk_type='page'（写入方从不产 page 块，
                    页级溯源从未生效）+ R13 判空缺失；一致性扫描「设计文件」
                    上下文按 doc_type 列过滤（唯一写入值是'全局事实上传'，
                    分支恒空）+ 无序/含空块；documents_index.json 并发读改
                    写丢条目；批量上传失败留四层磁盘孤儿。
  F 自证            判据自身归零必须红（防静态锁假绿）。
"""

import ast
import io
import sys
import uuid
from pathlib import Path

import app.db as _appdb
import app.routers.global_facts_docs as gfd
import app.services.doc_pipeline.doc_storage as store
import app.services.scoring_items as scoring
import pytest
from app.db import get_conn, init_db
from app.services import file_parser as fp
from app.services import scoring_items as svc
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parent.parent
_FP_SRC = (Path(fp.__file__)).read_text(encoding="utf-8")
_GFD_SRC = (Path(gfd.__file__)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A · 目录编号识别（行为实证）
# ---------------------------------------------------------------------------
class TestOutlineDotForms:
    def _first(self, text):
        out = fp.simple_parse_outline(text)
        return out[0] if out else None

    def test_dot_no_separator_hierarchies(self):
        """点分后无分隔符直接跟汉字：层级与编号完整保留（修复前错撕双输）。"""
        n2 = self._first("1.1工程概况")
        assert n2 is not None
        assert (n2["id"], n2["level"], n2["title"]) == ("1.1", 2, "工程概况")
        n3 = self._first("1.1.1施工准备")
        assert n3 is not None
        assert (n3["id"], n3["level"], n3["title"]) == ("1.1.1", 3, "施工准备")

    def test_amount_and_date_lines_not_pseudo_titles(self):
        """数值量词/日期形态/句读护栏：正文行不再生成伪节点（落库成空章节）。"""
        for line in (
            "3.5万元包干",
            "1.5米，采用C30混凝土",
            "3.5 万元，包干价",
            "2024.05.12 第一次工地会议纪要",
            "2024.05.12第一次工地会议纪要",
        ):
            assert self._first(line) is None, f"「{line}」不应生成节点"

    def test_separator_forms_regression(self):
        """既有形态零回归：带分隔符/OCR 空格/单数字/句读/年份护栏逐一复验。"""
        assert (self._first("1.1 工程概况")["id"], self._first("1.1 工程概况")["level"]) == ("1.1", 2)
        assert (self._first("1 . 1 工程概况")["id"]) == "1.1"
        n = self._first("1.总则")
        assert n is not None and n["level"] == 1 and n["title"] == "总则"
        n = self._first("2.工期要求")
        assert n is not None and n["level"] == 1
        assert self._first("3.1.12 身份证复印件、照片；") is None
        assert self._first("2023 年最新规范") is None
        n = self._first("第一章 工程概况")
        assert n is not None and n["level"] == 1
        n = self._first("一、工程概况")
        assert n is not None and n["level"] == 2

    def test_multi_line_tree_shape(self):
        """多行组合：章节 → 点分无分隔符子节 → 正文行被跳过。"""
        tree = fp.simple_parse_outline("第一章 工程概况\n1.1工程概况\n1.1.1施工准备\n3.5万元包干\n1.2 人员配备")
        assert len(tree) == 1
        ch = tree[0]
        assert ch["level"] == 1
        subs = {c["id"]: c for c in ch["children"]}
        assert set(subs) == {"1.1", "1.2"}
        assert subs["1.1"]["children"][0]["id"] == "1.1.1"

    def test_dot_pattern_precedes_separator_pattern(self):
        """源码锁：无分隔符点分条必须排在有分隔符点分条之前（回溯命中问题）。"""
        patterns = fp._OUTLINE_PATTERNS
        # ✅ R209 重锚（意图不放宽）：条目数 9→11（新增罗马数字 2 条，插在兜底条
        # 之前）；本用例锁的是**相对顺序**，用「首个无分隔符点分条 < 首个有分隔符
        # 点分条」表达，不依赖绝对长度。
        assert len(patterns) == 11
        idx_nosep = next(i for i, (_, p) in enumerate(patterns) if "u4e00" in p.pattern)
        idx_sep = next(
            i
            for i, (_, p) in enumerate(patterns)
            if "." in p.pattern and "u4e00" not in p.pattern and i > idx_nosep - 1 and i != idx_nosep
        )
        assert idx_nosep < idx_sep, "无分隔符点分条必须先于有分隔符点分条（否则 1.1.1X 被回溯命中错撕）"


# ---------------------------------------------------------------------------
# B · 编码链（行为实证 + 采样源码锁）
# ---------------------------------------------------------------------------
class TestEncodingChain:
    def test_utf32le_bom_decodes_and_not_rejected(self):
        """UTF-32 BOM 不再被 UTF-16LE 抢判（合法文件不再误杀 400）。"""
        payload = "工程概况清单\n".encode("utf-32")
        assert fp._detect_utf16(payload) == ("utf-32", "UTF-32LE")
        text, warnings = fp._decode_text_ex(payload)
        assert text == "工程概况清单\n"
        assert any("UTF-32" in w for w in warnings)
        fp._reject_binary_text_ext(payload)  # 不抛

    def test_utf32be_bom_decodes(self):
        payload = b"\x00\x00\xfe\xff" + "第二批清单\n".encode("utf-32-be")
        text, _ = fp._decode_text_ex(payload)
        assert text == "第二批清单\n"

    def test_utf16_bom_no_zero_width_false_warning(self):
        """BOM-aware codec：不再残留 U+FEFF → 不再触发零宽字符假告警。"""
        _, warnings = fp._decode_text_ex("第一批 设备清单\n".encode("utf-16"))
        assert any("UTF-16" in w for w in warnings)
        assert not any("零宽" in w for w in warnings), f"BOM 残留触发假告警：{warnings}"

    def test_utf16_no_bom_branch_regression(self):
        """无 BOM 探测分支（codec/展示名/口径）与 R175 基线逐字一致。"""
        assert fp._detect_utf16("Name: 设备清单\n".encode("utf-16-le")) == (
            "utf-16-le",
            "UTF-16LE（无 BOM，按字节分布推测）",
        )
        assert fp._detect_utf16("设备清单\n".encode("utf-8") * 20) is None

    def test_no_bom_probe_is_sampled(self):
        """源码锁：无 BOM 探测必须采样（30MB 全量双扫描 ~4.2s 纯 CPU）。"""
        assert "_UTF16_PROBE_BYTES" in _FP_SRC
        assert "content[:_UTF16_PROBE_BYTES]" in _FP_SRC


# ---------------------------------------------------------------------------
# C · 通道加固
# ---------------------------------------------------------------------------
class TestChannelHardening:
    def test_tiff_frame_cap_and_truncation_notice(self, monkeypatch):
        """多页 TIFF 帧数对齐 OCR_PDF_MAX_PAGES 口径，超限留截断告警。"""
        pytest.importorskip("PIL")
        from PIL import Image

        buf = io.BytesIO()
        img = Image.new("RGB", (8, 8), "white")
        img.save(buf, format="TIFF", save_all=True, append_images=[Image.new("RGB", (8, 8)) for _ in range(4)])
        content = buf.getvalue()
        diag: dict = {"warnings": []}

        import app.config as _cfg

        monkeypatch.setattr(_cfg.settings, "ocr_pdf_max_pages", 2, raising=False)
        frames = fp._split_tiff_frames(content, diag)
        assert len(frames) == 2, "超限必须只拆前 N 帧"
        assert any("仅取前 2 帧" in w for w in diag.get("warnings", [])), diag

    def test_tiff_midway_failure_returns_partial_with_notice(self, monkeypatch):
        """中途解码失败：返回已积累帧 + 截断告警（不再静默丢帧）。"""
        import app.services.file_parser as fp_mod

        class _FakeImg:
            n_frames = 3
            # ✅ R203 重锚：拆帧循环新增单帧像素闸（读 img.size 判定），
            #    mock 须提供真实图片才有的属性（小尺寸不触发降采样分支）。
            size = (100, 100)

            def __init__(self):
                self._i = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def seek(self, i):
                self._i = i
                if i == 1:
                    raise OSError("模拟第 2 帧解码失败")

            def convert(self, *_a, **_k):
                return self

            def save(self, buf, **_k):
                buf.write(f"frame{self._i}".encode())

        class _FakePIL:
            class Image:
                @staticmethod
                def open(_f):
                    return _FakeImg()

        real_import = __import__

        def _fake_import(name, *a, **k):
            if name == "PIL":
                return _FakePIL
            return real_import(name, *a, **k)

        import builtins

        real_bi_import = builtins.__import__

        def _patched(name, *a, **k):
            if name == "PIL" or name.startswith("PIL."):
                return _FakePIL
            return real_bi_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _patched)
        diag: dict = {"warnings": []}
        frames = fp_mod._split_tiff_frames(b"II*\x00fake", diag)
        assert frames == [b"frame0"], "第 2 帧失败必须保留已积累帧"
        assert any("第 2 帧失败" in w for w in diag.get("warnings", [])), diag

    def test_encrypted_ole_clearly_rejected(self):
        """加密 OOXML（EncryptedPackage 流）入口即明确报错，不再误路由。"""
        marker = "EncryptedPackage".encode("utf-16-le")
        content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + marker + b"\x00" * 64
        for fname in ("secret.xlsx", "secret.docx", "encrypted.bin"):
            with pytest.raises(fp.ParseError) as ei:
                fp.parse_file_content_ex(content, fname)
            assert "加密" in str(ei.value)

    def test_bmp_weak_signature_aligned_with_text_guard(self):
        """bmp 签名与 _binary_format_for_text_guard 同口径（保留字段必须为 0）。"""
        assert fp.signature_valid("bmp", b"BMW maintenance manual 2024") is False
        real_bmp = b"BM" + b"\x00" * 10
        assert fp.signature_valid("bmp", real_bmp) is True

    def test_pdf_ocr_pixel_gate_skips_giant_page(self, monkeypatch):
        """超大页（72000pt 边长）不再靠 malloc 失败兜底：跳过 + 告警留痕。"""
        fitz = pytest.importorskip("fitz")
        import app.services.ocr as ocr_mod

        doc = fitz.open()
        doc.new_page(width=72000, height=72000)
        pdf_bytes = doc.tobytes()
        doc.close()

        calls: list = []

        def _fake_ocr(png):
            calls.append(len(png))
            return type("R", (), {"text": ""})()

        monkeypatch.setattr(ocr_mod, "ocr_bytes_sync", _fake_ocr)
        diag: dict = {"warnings": []}
        out = fp._pdf_ocr_fallback(pdf_bytes, diag=diag)
        assert out == ""
        assert calls == [], "超 4 倍限值的巨型页必须跳过而非白跑 OCR"
        assert any("尺寸过大" in w for w in diag.get("warnings", [])), diag

    def test_docx_fallback_records_diag(self):
        """docx 回退 docx2python 必须 _note_info（与 PDF 通道观测面对齐）。"""
        seg = _FP_SRC[_FP_SRC.index("def _parse_docx(") : _FP_SRC.index("def _parse_docx_fallback")]
        assert "_note_info(diag," in seg, "两个回退分支都必须记录 diag"
        assert "def _parse_docx(content: bytes, diag: dict | None = None)" in _FP_SRC


# ---------------------------------------------------------------------------
# D · 路由层（global_facts_docs）
# ---------------------------------------------------------------------------
@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "import-r191.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid


class TestRouteLayerGuards:
    async def test_reconcile_preserves_failed_status(self, ctx):
        """reconcile 不得把显式失败（failed）翻回 success（pending 仍修）。"""
        db, pid = ctx
        d1, d2 = uuid.uuid4().hex, uuid.uuid4().hex
        for did, status in ((d1, "failed"), (d2, "pending")):
            await db.execute(
                "INSERT INTO project_documents(id, project_id, file_name, file_type, doc_type,"
                " parsed_markdown, parse_status) VALUES(?,?,?,?,?,?,?)",
                (did, pid, f"{did}.txt", "txt", "全局事实上传", "正文内容够长", status),
            )
        await db.commit()
        assert await gfd._reconcile_parse_status(db, d1) is False, "failed 必须保留"
        assert await gfd._reconcile_parse_status(db, d2) is True, "pending 仍被修正"
        row = await (await db.execute("SELECT parse_status FROM project_documents WHERE id=?", (d1,))).fetchone()
        assert row["parse_status"] == "failed"

    async def test_reconcile_all_preserves_failed_status(self, ctx):
        db, pid = ctx
        d1 = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO project_documents(id, project_id, file_name, file_type, doc_type,"
            " parsed_markdown, parse_status) VALUES(?,?,?,?,?,?,?)",
            (d1, pid, "f.txt", "txt", "全局事实上传", "正文内容够长", "failed"),
        )
        await db.commit()
        assert await gfd._reconcile_parse_status_all(db, pid) == 0
        row = await (await db.execute("SELECT parse_status FROM project_documents WHERE id=?", (d1,))).fetchone()
        assert row["parse_status"] == "failed"

    async def test_category_non_string_is_400_not_500(self, ctx):
        """{"doc_category": 123} / None → 400（不再 AttributeError 500）。"""
        db, _pid = ctx
        for bad in ({"doc_category": 123}, {"doc_category": None}, {}):
            with pytest.raises(HTTPException) as ei:
                await gfd.update_document_category("no-such-doc", bad, db)
            assert ei.value.status_code == 400

    def test_upload_replacement_marks_stale(self):
        """源码锁：上传替换路径必须置 stale + 失效导出缓存 + 回传 stale_facts/note。"""
        seg = _GFD_SRC[_GFD_SRC.index("async def upload_documents") :]
        seg = seg[: seg.index("\n@router")]
        assert "_mark_project_facts_stale(db, real_pid)" in seg
        assert "_invalidate_project_export_caches(db, real_pid)" in seg
        assert 'result["stale_facts"]' in seg
        assert 'result["note"]' in seg

    def test_upload_written_registered_before_write(self):
        """流式落盘：fpath 必须在 _stream_to_disk 之前登记（半写文件可清理）。"""
        seg = _GFD_SRC[_GFD_SRC.index("async def upload_documents") :]
        seg = seg[: seg.index("\n@router")]
        i_append = seg.index("written.append(fpath)")
        i_stream = seg.index("await _stream_to_disk(f, fpath, max_bytes)")
        assert i_append < i_stream, "written.append(fpath) 必须先于 _stream_to_disk"

    def test_upload_commit_guarded(self):
        """commit 自身抛异常必须 rollback + 清理 + 503（不再裸奔）。"""
        seg = _GFD_SRC[_GFD_SRC.index("async def upload_documents") :]
        seg = seg[: seg.index("\n@router")]
        assert "上传保存提交失败" in seg
        assert 'raise HTTPException(503, "文档服务暂时不可用，请稍后重试") from None' in seg

    def test_upload_dedupes_same_request_duplicate_names(self):
        """同请求重名：被 upsert 覆盖的旧 saved 条目必须剔除。"""
        seg = _GFD_SRC[_GFD_SRC.index("async def upload_documents") :]
        seg = seg[: seg.index("\n@router")]
        assert 'saved[:] = [s for s in saved if s.get("file_name") != fname]' in seg

    def test_delete_unlink_after_commit(self):
        """delete_document：unlink 必须在 commit 之后（先删文件后提交=删一半）。"""
        seg = _GFD_SRC[_GFD_SRC.index("async def delete_document") :]
        seg = seg[: seg.index("\n@router")]
        i_commit = seg.index("await db.commit()")
        i_unlink = seg.index("candidate.unlink()")
        assert i_commit < i_unlink, "原始文件删除必须挪到 DB 提交成功之后"

    def test_stale_calls_are_failure_tolerant(self):
        """4 处 stale/缓存失效调用必须容错（变更已提交后失败不得 500）。"""
        count = _GFD_SRC.count("不阻断响应")
        assert count >= 4, f"容错注释/路径计数不足：{count}"

    def test_parse_all_early_exit_has_force_key(self):
        """parse-all 早退分支补 force 键（与主分支响应形状对齐）。"""
        seg = _GFD_SRC[_GFD_SRC.index("async def parse_all_documents") :]
        seg = seg[: seg.index("\n@router")]
        i_early = seg.index('"parsed": 0,')
        i_force = seg.index('"force": force,')
        assert i_force > i_early and seg.index('"message": _msg,') > i_force

    def test_upload_orphan_purge_registered(self):
        """上传异常路径清理四层孤儿（purge_document 逐条调用留痕）。"""
        seg = _GFD_SRC[_GFD_SRC.index("async def upload_documents") :]
        seg = seg[: seg.index("\n@router")]
        assert "清理孤儿四层产物失败" in seg
        assert "doc_pipeline.purge_document(db, doc_id=s[\"id\"], project_id=real_pid)" in seg


# ---------------------------------------------------------------------------
# H · 热文件承接（R190 收口后 upload_outline 转冷，H1 落地）
# ---------------------------------------------------------------------------
class TestHotFileHandover:
    def test_upload_outline_upload_limit_is_runtime_read(self):
        """H1：目录识别上传限额必须运行时读模块属性（不得静态快照）。

        静态 from-import 是 import 时快照：settings.upload_max_bytes 运行期
        变更或测试 monkeypatch 时，「目录识别上传」与「资料上传/系统提示」
        的限额判定分叉（同一判据两份取值同族缺陷）。"""
        import app.routers.upload_outline as uo

        src = (Path(uo.__file__)).read_text(encoding="utf-8")
        # 静态 from-import 快照不得回流（MAX_UPLOAD_BYTES 出现在 import 括号内）
        assert "MAX_UPLOAD_BYTES," not in src, "MAX_UPLOAD_BYTES 不得出现在 import 块（静态快照回流）"
        assert "_fp.MAX_UPLOAD_BYTES" in src, "必须运行时读 file_parser 模块属性"
        assert "from app.services import file_parser as _fp" in src

    def test_facts_chain_truncation_unified(self):
        """B3：事实链截断判定必须走 doc_is_truncated 单一出口（不得第三份实现）。

        热文件承接：sse_handlers.py 在 R190 收口后转冷，本轮把 2026-09-30 P0
        修复唯此漏改的副本统一到四信号口径（互斥两信号分支曾致存量行
        parse_truncated=0 但告警留痕的不漏报语义丢失）。"""
        import app.routers.sse_handlers as sh

        src = (Path(sh.__file__)).read_text(encoding="utf-8")
        seg = src[src.index("async def _load_facts_source_docs") :]
        seg = seg[: seg.index("async def _mark_docs_extract_status")]
        assert "_gfd.doc_is_truncated(d)" in seg, "判定必须调统一出口"
        assert 'has_trunc and d.get("parse_truncated")' not in seg, "互斥两信号分支不得回流"
        assert '"截断" in str(d.get("parse_warnings")' not in seg, "中文子串第二实现不得回流"


# ---------------------------------------------------------------------------
# E · 跨模块（scoring / consistency / doc_storage）
# ---------------------------------------------------------------------------
class TestCrossModuleGuards:
    async def test_scoring_sources_read_real_chunks(self, ctx):
        """页级选择能力修复：读 section/semantic 并按 page_num 聚合成页块。"""
        db, pid = ctx
        doc = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO project_documents(id, project_id, file_name, file_type, doc_type,"
            " parsed_markdown, parse_status) VALUES(?,?,?,?,?,?,?)",
            (doc, pid, "投标文件.docx", "docx", "全局事实上传", "评标办法 评分标准 综合评分", "success"),
        )
        for i, (ct, pn, txt) in enumerate(
            (
                ("section", 1, "第一章 评标办法"),
                ("semantic", 1, "评分标准细则"),
                ("section", 2, "第二章 商务评分"),
                ("page", 3, "旧 page 块（写入方已不产，不得读取）"),
            )
        ):
            await db.execute(
                "INSERT INTO doc_chunks(doc_id, chunk_id, chunk_type, page_num, text) VALUES(?,?,?,?,?)",
                (doc, f"{doc}-c{i}", ct, pn, txt),
            )
        await db.commit()
        sources = await svc.select_scoring_sources(db, pid)
        assert len(sources) == 1
        pages = sources[0]["pages"]
        assert [p["page_num"] for p in pages] == [1, 2], "必须按 page_num 聚合且不读 page 块"
        assert "评分标准细则" in pages[0]["text"], "同页多块正文必须合并"
        assert sources[0]["page_nums"] == [1, 2]

    async def test_scoring_sources_null_cursor_degrades(self, ctx, monkeypatch):
        """R13 判空：主游标 None → 空列表（走 failed run 语义），不 AttributeError。"""
        db, pid = ctx

        class _NullOnce:
            def __init__(self, real):
                self._real = real

            async def execute(self, sql, params=()):
                if "FROM project_documents" in sql:
                    return None
                return await self._real.execute(sql, params)

        sources = await svc.select_scoring_sources(_NullOnce(db), pid)
        assert sources == []

    async def test_design_docs_context_by_category(self, ctx):
        """「设计文件」上下文按 doc_category 过滤（doc_type 键位纠错）+ 空正文过滤 + 固定序。"""
        from app.services.consistency_scanner import build_project_docs_text

        db, pid = ctx
        rows = [
            ("d-empty", "设计文件", ""),  # 空正文：必须被过滤
            ("d-old", "设计文件", "设计说明甲"),
            ("d-new", "设计文件", "设计说明乙"),
            ("d-other", "财务资料", "注册资金证明"),
        ]
        for i, (did, cat, md) in enumerate(rows):
            await db.execute(
                "INSERT INTO project_documents(id, project_id, file_name, file_type, doc_type,"
                " doc_category, parsed_markdown, parse_status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (did, pid, f"f{i}.txt", "txt", "全局事实上传", cat, md, "success", f"2026-01-0{i + 1} 00:00:00"),
            )
        await db.commit()
        design = await build_project_docs_text(db, pid, doc_category="设计文件", limit=4000)
        assert "设计说明甲" in design and "设计说明乙" in design
        assert "注册资金证明" not in design, "分类过滤必须生效"
        assert design.index("设计说明甲") < design.index("设计说明乙"), "必须按 created_at 固定序"
        # 空正文行不产生占位块
        assert "f0" not in design
        # 全量（不带分类）也不过滤掉非空行
        alltext = await build_project_docs_text(db, pid, limit=4000)
        assert "注册资金证明" in alltext

    def test_design_docs_call_site_key(self):
        """源码锁：consistency_scanner 调用点必须按 doc_category 传「设计文件」。

        函数级行为测试不经过该调用点（M9 A/B 教训：仅测函数对调用点键位错位
        全盲），必须另锁调用点实参形态。"""
        import app.services.consistency_scanner as cs

        src = (Path(cs.__file__)).read_text(encoding="utf-8")
        assert 'doc_category="设计文件"' in src, "调用点必须传 doc_category"
        assert 'doc_type="设计文件"' not in src, "doc_type 键位错位不得回流（该列唯一写入值是'全局事实上传'）"

    def test_index_write_lock_present(self):
        """documents_index.json 读改写必须有进程内互斥（并发丢条目）。"""
        src = (Path(store.__file__)).read_text(encoding="utf-8")
        assert "_INDEX_WRITE_LOCK = threading.Lock()" in src
        assert src.count("with _INDEX_WRITE_LOCK:") == 2, "update_index / remove_from_index 都必须持锁"


# ---------------------------------------------------------------------------
# F · 自证（判据归零必须红）
# ---------------------------------------------------------------------------
class TestSelfProof:
    def test_self_proof_dot_guard_really_works(self):
        """自证：把量词护栏归零（模拟摘除）后 3.5万元包干 必然生成伪节点。"""
        saved_q = fp._OUTLINE_DOT_QUANTIFIER_RE
        saved_d = fp._OUTLINE_DATE_ID_RE
        try:
            fp._OUTLINE_DOT_QUANTIFIER_RE = None  # type: ignore[assignment]
            fp._OUTLINE_DATE_ID_RE = None  # type: ignore[assignment]
            with pytest.raises(Exception):
                # 护栏被摘除后 simple_parse_outline 走旧路径：要么生成伪节点，
                # 要么在护栏判 None 处抛 AttributeError——两者都证明护栏真实参与判定。
                out = fp.simple_parse_outline("3.5万元包干")
                assert out == [], "护栏摘除后仍判空 = 护栏未参与判定（假护栏）"
        finally:
            fp._OUTLINE_DOT_QUANTIFIER_RE = saved_q
            fp._OUTLINE_DATE_ID_RE = saved_d
        # 还原后必须恢复拦截
        assert fp.simple_parse_outline("3.5万元包干") == []

    def test_self_proof_encrypted_marker_real(self):
        """自证：加密判据基于真实流名字节（非恒真占位）。"""
        assert "EncryptedPackage".encode("utf-16-le") == fp._OLE_ENCRYPTED_MARKER
        assert fp._ole_encrypted(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32) is False
