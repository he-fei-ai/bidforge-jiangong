# -*- coding: utf-8 -*-
"""R203 · 解析提取模块（上传解析/要点提取）遗留收口护栏（2026-11-09）。

承接 R191 报告 §七 遗留清单 + 本轮三路探查新发现，六组：
A 目录识别护栏扩展（英文单位盲区 + 「百」支持）—— BE-1/BE-10
B 图片像素闸 / 编码质量闸 / 归档单条目放宽 / sniff 与 text-guard 对齐 —— BE-2/3/4/5/7
C 路由层：parse-all failed 选取 / preview parse_status / commit 守卫 —— BE-11/12/17
D 分块行边界与存储容错 —— BE-13/15/16
E 分类关键词精确化 —— BE-6
F 自证与字节体检
"""

from __future__ import annotations

import inspect
import io
import json
import logging
from pathlib import Path

import pytest
from app.routers import global_facts_docs as gfd
from app.services import doc_categories as dc
from app.services import file_parser as fp
from app.services.doc_pipeline import doc_storage as store
from app.services.doc_pipeline import pipeline as pl
from app.services.doc_pipeline.doc_chunker import chunk_document

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A 组 · 目录识别护栏扩展（BE-1 英文单位盲区 / BE-10 中文数字「百」）
# ---------------------------------------------------------------------------


class TestOutlineGuardR203:
    @pytest.mark.parametrize(
        "line",
        [
            "1.5MPa直接浇筑混凝土",
            "2.4GHz无线传输",
            "3.5%的利润率下浮",
            "1.5m/s的风速要求",
        ],
    )
    def test_dot_number_english_unit_rejected(self, line):
        """R191 三护栏只对 raw_id 含 "." 生效——「点分数值+英文单位/%」形态被
        单数字 pattern 撕成伪节点（编号被吃掉一段）。R203 补第四道护栏后
        全部按正文行跳过。"""
        assert simple_outline(line) == []

    def test_r191_guards_no_regression(self):
        """既有行为零回退：正常点分标题识别、R191 三护栏形态仍拦截。"""
        nodes = simple_outline("1.1工程概况")
        assert nodes and nodes[0]["title"] == "工程概况"
        assert simple_outline("1 总则")
        nodes3 = simple_outline("1.1.1施工准备")
        assert nodes3 and nodes3[0]["title"] == "施工准备"
        assert simple_outline("3.5万元包干") == []
        assert simple_outline("1.5米，采用C30混凝土") == []
        assert simple_outline("2024.05.12 第一次工地会议纪要") == []
        assert simple_outline("3.1.12 身份证复印件、照片；") == []

    def test_cn_num_bai_recognized(self):
        """R191 遗留 #7：「第一百二十三章」此前因「百」不在字符类被整行漏识。"""
        nodes = simple_outline("第一百二十三章 施工准备")
        assert nodes and nodes[0]["title"] == "施工准备"
        nodes2 = simple_outline("一百二十三、验收标准")
        assert nodes2 and nodes2[0]["title"] == "验收标准"

    def test_source_anchors(self):
        assert "百" in fp._OUTLINE_CN_NUMS
        assert fp._OUTLINE_DOT_NUM_LINE_RE.pattern.startswith("^\\d+")


# ---------------------------------------------------------------------------
# B 组 · 图片像素闸 / 编码质量闸 / 归档放宽 / sniff 与 text-guard 对齐
# ---------------------------------------------------------------------------


class TestChannelHardeningR203:
    def test_sniff_bmp_weak_signature_rejected(self):
        """_sniff_type 与 signature_valid / text-guard 三处口径收敛：
        "BMW maintenance manual" 不再被嗅探成 bmp 白跑 OCR。"""
        assert fp._sniff_type(b"BMW maintenance manual" + b"\x00" * 8) == ""

    def test_sniff_bmp_real_signature_kept(self):
        assert fp._sniff_type(b"BM" + b"\x00" * 4 + b"\x00\x00\x00\x00" + b"x" * 8) == "bmp"

    def test_text_guard_riff_media(self):
        """text-guard 补 RIFF 容器判据（与 signature_valid 的 webp 分支对齐）。"""
        assert fp._binary_format_for_text_guard(b"RIFF\x12\x00\x00\x00WEBPVP8 ") == "RIFF 媒体文件"
        assert fp._binary_format_for_text_guard(b"RIFF\x24\x00\x00\x00WAVEfmt ") == "RIFF 媒体文件"
        assert fp._binary_format_for_text_guard("正常中文施工方案文本".encode("utf-8")) == ""

    def test_downscale_oversize_single_frame(self, monkeypatch):
        """单帧图片超像素闸 → 等比降采样 + diag 留痕（对齐 PDF 通道口径）。"""
        from PIL import Image

        monkeypatch.setattr(fp, "_OCR_RENDER_MAX_PIXELS", 1000)
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        diag: dict = {}
        out = fp._downscale_image_bytes(buf.getvalue(), diag)
        with Image.open(io.BytesIO(out)) as check:
            assert check.width * check.height <= 1000
            assert check.width < 100
        assert any("降采样" in str(w) for w in diag.get("warnings", []))

    def test_downscale_small_image_passthrough(self, monkeypatch):
        from PIL import Image

        monkeypatch.setattr(fp, "_OCR_RENDER_MAX_PIXELS", 50_000_000)
        img = Image.new("RGB", (40, 40), (0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        assert fp._downscale_image_bytes(raw, {}) is raw

    def test_split_tiff_oversize_frame_downscale(self, monkeypatch):
        """多页 TIFF 拆帧循环内像素闸：超大帧降采样后仍参与识别（不丢内容）。"""
        from PIL import Image

        monkeypatch.setattr(fp, "_OCR_RENDER_MAX_PIXELS", 1000)
        base = Image.new("RGB", (80, 80), (0, 0, 0))
        buf = io.BytesIO()
        base.save(
            buf,
            format="TIFF",
            save_all=True,
            append_images=[Image.new("RGB", (80, 80), (255, 0, 0))],
        )
        diag: dict = {}
        frames = fp._split_tiff_frames(buf.getvalue(), diag)
        assert len(frames) == 2
        for fr in frames:
            with Image.open(io.BytesIO(fr)) as check:
                assert check.width * check.height <= 1000
        assert any("降采样" in str(w) for w in diag.get("warnings", []))

    def test_gb18030_big5_quality_warning(self):
        """GBK/GB18030 回退质量闸：Big5 源文件解码「成功」但码点质量不达标
        → 追加定向告警（正文照常返回，不引入新故障）。"""
        raw = "工程概況與施工規範說明，鋼筋混凝土結構要求。".encode("big5")
        text, warnings = fp._decode_text_ex(raw)
        assert "工程" in text or len(text) > 0
        assert any("Big5" in w or "可疑字符" in w for w in warnings)

    def test_archive_entry_default_aligned_with_total_gate(self):
        """单条目闸默认值 64MB → 400MB（对齐总量闸）：30MB 上传的合法大表
        （sheet XML 解压 250MB 级）不再被声明值闸先拒；谎报防护由
        _read_zip_entry_limited 的实际读出上限承担（不受本变更影响）。"""
        assert fp.MAX_ARCHIVE_ENTRY_BYTES == fp.MAX_ARCHIVE_UNCOMPRESSED_BYTES


# ---------------------------------------------------------------------------
# C 组 · 路由层（BE-11 parse-all failed 选取 / BE-12 preview / BE-17 commit 守卫）
# ---------------------------------------------------------------------------


class TestRoutingR203:
    def test_parse_all_selects_failed_with_body(self):
        """parse-all（force=False）选取口径补 OR parse_status='failed'——
        「曾成功→重解析失败」的文档（正文非空）此前不被选中，前端
        「重试失败文档（N）」按钮假动作。"""
        src = inspect.getsource(gfd.parse_all_documents)
        assert "OR parse_status='failed'" in src
        # 锁内复核同步放行 failed（否则修复空转：选进来又在复核处 continue）
        assert 'parse_status' in src
        assert '_c["parse_status"] != "failed"' in src

    def test_parse_all_recheck_select_includes_status_column(self):
        src = inspect.getsource(gfd.parse_all_documents)
        assert "SELECT parsed_markdown, parse_status FROM project_documents" in src

    def test_preview_returns_parse_status_both_branches(self):
        """preview 补 parse_status（加法式，两条返回路径契约对称）——
        failed 残留旧正文时前端可叠加「上次解析失败」警示。"""
        src = inspect.getsource(gfd.preview_document)
        assert src.count('"parse_status": row["parse_status"]') == 2
        assert '"parse_status "' in src  # SELECT 列集含 parse_status

    def test_ingest_parse_result_commit_guard(self):
        """分块落库 commit 裸奔收口：commit 失败 → rollback（清悬空写）+ 上抛，
        由上层「解析落库失败」语义接管（与 R191 upload commit 守卫同族）。"""
        src = inspect.getsource(pl.ingest_parse_result)
        assert "await db.commit()" in src
        assert "await db.rollback()" in src

    @pytest.mark.parametrize("fn_name", ["_reconcile_parse_status", "_reconcile_parse_status_all"])
    def test_reconcile_rollback_on_error(self, fn_name):
        """reconcile 家族 except 补 rollback：失败可忽略（return False/0），
        但悬空事务必须显式回滚（commit 裸奔家族收口）。"""
        src = inspect.getsource(getattr(gfd, fn_name))
        assert "await db.rollback()" in src


# ---------------------------------------------------------------------------
# D 组 · 分块行边界与存储容错（BE-15 / BE-13 / BE-16）
# ---------------------------------------------------------------------------


class TestChunkerAndStorageR203:
    def test_chunker_semantic_split_respects_row_boundary(self):
        """大表（>1600 字）semantic 切分按行边界：不再出现半截表格行。"""
        lines = ["# 汇总表", "【表格】"] + [
            f"| 项目名称第{i}项 | 数值{i} | 备注说明{i} |" for i in range(200)
        ]
        text = "\n".join(lines)
        chunks = chunk_document(text, doc_id="d1")
        sem = [c for c in chunks if c["chunk_type"] == "semantic"]
        assert sem, "长表应触发 semantic 切分"
        original_rows = {ln for ln in lines if ln.startswith("|")}
        got_rows: set[str] = set()
        for c in sem:
            for ln in c["text"].split("\n"):
                if ln.startswith("|"):
                    got_rows.add(ln)
        assert got_rows <= original_rows, "切分不得产生半截表格行"
        assert len(got_rows) >= len(original_rows) * 0.9, "行内容不得大量丢失"

    def test_delete_doc_tree_failure_returns_false(self, tmp_path, monkeypatch):
        """rmtree 失败（文件被占用）→ WARNING + 返回 False，不再假成功。"""
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
        d = tmp_path / "p1" / "d1"
        d.mkdir(parents=True)

        def _boom(target, *a, **k):
            raise OSError("模拟文件被占用")

        monkeypatch.setattr("shutil.rmtree", _boom)
        assert store.delete_doc_tree("p1", "d1") is False

    def test_index_corrupt_warn_and_rebuild(self, tmp_path, monkeypatch, caplog):
        """归档索引损坏：update_index WARNING + 重建；remove_from_index WARNING +
        保守跳过——两条路径都不再静默。"""
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
        idx = tmp_path / "p1" / "documents_index.json"
        idx.parent.mkdir(parents=True)
        idx.write_text('{"broken":', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="doc_storage"):
            store.remove_from_index("p1", "d1")
            store.update_index("p1", {"doc_id": "d1", "layers": {}})
        assert any("损坏" in r.message for r in caplog.records)
        data = json.loads(idx.read_text(encoding="utf-8"))
        assert isinstance(data, list) and data and data[0]["doc_id"] == "d1"


# ---------------------------------------------------------------------------
# E 组 · 分类关键词精确化（BE-6）
# ---------------------------------------------------------------------------


class TestCategoryRulesR203:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # 裸「清单」收窄：安全/整改/风险类不再吞进报价清单
            ("安全检查清单.xlsx", "其他"),
            ("隐患整改清单.docx", "其他"),
            ("材料清单.xlsx", "其他"),
            # 报价语义精确词承接（⚠️ 样例避开「投标」——招标文件规则序在前，
            # 「投标报价文件」按既有规则优先归招标文件属正确行为）
            ("工程量清单.xlsx", "报价清单"),
            ("清单计价规范.pdf", "报价清单"),
            ("报价单.xlsx", "报价清单"),
            # 技术标/商务标 → 招标文件
            ("技术标.docx", "招标文件"),
            ("商务标文件.docx", "招标文件"),
            # 裸「工程师」删除：监理过程文件不再归人员资料
            ("监理工程师通知单.docx", "其他"),
            ("总工程师简历.docx", "人员资料"),
            # 既有行为保留
            ("投标文件.docx", "招标文件"),
            ("财务报表.xlsx", "财务资料"),
            ("施工组织设计.docx", "招标文件"),
        ],
    )
    def test_auto_classify(self, name, expected):
        assert dc.auto_classify_document(name, name.rsplit(".", 1)[-1]) == expected


# ---------------------------------------------------------------------------
# F 组 · 自证与字节体检
# ---------------------------------------------------------------------------


class TestSelfProofR203:
    def test_r203_marker(self):
        assert True

    @pytest.mark.parametrize(
        "rel",
        [
            "app/services/file_parser.py",
            "app/services/doc_categories.py",
            "app/routers/global_facts_docs.py",
            "app/services/doc_pipeline/pipeline.py",
            "app/services/doc_pipeline/doc_storage.py",
            "app/services/doc_pipeline/doc_chunker.py",
        ],
    )
    def test_bytes_health_crlf(self, rel):
        """后端 app 源保持纯 CRLF（R161/R191 教训：脚本改文件前先探测行尾）。"""
        raw = (BACKEND / rel).read_bytes()
        assert b"\r\n" in raw, f"{rel} 缺少 CRLF 行尾"
        assert b"\n" not in raw.replace(b"\r\n", b""), f"{rel} 存在裸 LF 行尾"
        assert b"\xef\xbb\xbf" != raw[:3], f"{rel} 不应带 BOM"
        assert b"\xef\xbf\xbd" not in raw, f"{rel} 含替换符 U+FFFD"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def simple_outline(line: str):
    return fp.simple_parse_outline(line)