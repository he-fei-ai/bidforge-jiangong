"""R136（2026-10-27）解析提取模块专项护栏。

覆盖三组本轮修复，全部为「引入前必红、引入后必绿」的反向锚：

A 组 —— .tif 格式缺口（TIFF 两个官方扩展名）+ 四层白名单同源
B 组 —— parse_coverage 恒定 1.0（总页数与已解析页数同源）
C 组 —— 批量解析重试口径分叉 + 断点续跑丢诊断（checkpoint diag 往返）
D 组 —— 上传响应 saved[] 与列表/预览的字段集对称（跨接口数据传递）

⚠️ 本文件刻意不新建任何测试数据库：统一复用 conftest 的 `db_conn`
   （内存库 + SCHEMA_SQL + db._migrate），保证测schema 与生产一致。
"""
import inspect
import json
import re
import uuid

import app.routers.global_facts_docs as gf_docs
import app.routers.upload_outline as upload_outline
import app.services.file_parser as fp
import pytest
from app.services.doc_pipeline import pipeline as doc_pipeline

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - 环境缺依赖时跳过 PDF 用例
    fitz = None


# ---------------------------------------------------------------------------
# A 组：.tif 格式缺口
# ---------------------------------------------------------------------------

def test_tif_declared_and_signed():
    """A1：.tif 必须进入对外声明集、且二进制格式必须有文件头签名。

    TIFF 的两个官方扩展名是 .tif / .tiff，扫描仪默认输出前者。引入前
    SUPPORTED_EXTENSIONS 只有 tiff ⇒ 用户上传扫描件直接被判「不支持格式」。
    """
    assert "tif" in fp.SUPPORTED_EXTENSIONS
    # 分派全集与对外声明必须仍是同一对象（R72 单一事实源，防再次漂移）
    assert fp._SUPPORTED_EXTENSIONS is fp.SUPPORTED_EXTENSIONS
    # 二进制格式必须登记签名（否则 test_upload_format_contract 会红）
    sigs = fp._BINARY_SIGNATURES["tif"]
    assert b"II*\x00" in sigs and b"MM\x00*" in sigs
    assert fp.signature_valid("tif", b"II*\x00\x2a\x00") is True
    assert fp.signature_valid("tif", b"MM\x00*\x00\x00") is True
    # 扩展名与内容不符必须拒绝（.tif 改名成别的二进制）
    assert fp.signature_valid("tif", b"%PDF-1.7") is False


def test_tif_dispatch_reaches_image_ocr(monkeypatch):
    """A2（功能实证）：扫描件 .tif 必须被分派到图片 OCR 通道。

    仅断言白名单不够 —— 白名单放行但分派 tuple 漏项时，会掉进 `else`
    分支被当文本解码成乱码。此处用桩替换 `_parse_image_ocr`，
    直接锁死「真实走到哪条通道、拿到什么 ftype」。
    """
    seen: list[str] = []

    def _fake_ocr(content, ftype, diag):
        seen.append(ftype)
        diag["warnings"] = list(diag.get("warnings") or [])
        return "<!-- page:1 -->\nOCR 识别文本"

    monkeypatch.setattr(fp, "_parse_image_ocr", _fake_ocr)
    text, diag = fp.parse_file_content_ex(b"II*\x00" + b"\x00" * 64, "扫描件.tif")
    assert seen == ["tif"], f".tif 未走图片 OCR 通道，实际分派: {seen}"
    assert diag["file_type"] == "tif"
    assert "OCR 识别文本" in text


def test_tif_multi_frame_and_ocr_penalty_wired():
    """A3：多帧 TIFF 分支与目录识别 OCR 噪声惩罚都必须覆盖 tif。

    - `_parse_image_ocr` 内 `if ftype == "tiff"` 若只认 tiff，.tif 多页扫描件
      只剩第 1 帧（其余页静默丢失）；
    - `parse_outline` 的 is_image 若漏 tif，.tif 扫描件不触发 OCR 噪声惩罚
      （confidence 虚高 0.1），与 .tiff 同源文件给出不同置信度。
    """
    ocr_src = inspect.getsource(fp._parse_image_ocr)
    assert re.search(r'if ftype in \("tiff",\s*"tif"\)', ocr_src), \
        "_parse_image_ocr 的多帧分支未覆盖 .tif"

    outline_src = inspect.getsource(upload_outline.parse_outline)
    m = re.search(r"is_image = ftype in \(([^)]*)\)", outline_src)
    assert m, "未能定位 parse_outline 的 is_image 判定"
    assert '"tif"' in m.group(1), "目录识别 OCR 噪声惩罚漏了 .tif"


# ---------------------------------------------------------------------------
# B 组：parse_coverage 独立来源
# ---------------------------------------------------------------------------

def test_count_page_markers_dedupes_pages():
    """B1：页数口径 = 「出现过的不同页号个数」（不是标记总数）。

    主通道 + OCR 兜底通道拼接时同一页号会出现两次，标记总数会虚增一倍。
    """
    assert fp.count_page_markers("") == 0
    assert fp.count_page_markers("纯文本无标记") == 0
    assert fp.count_page_markers("<!-- page:1 -->\na\n<!-- page:2 -->\nb") == 2
    # 同页号重复出现（两条通道拼接）仍算 1 页
    assert fp.count_page_markers(
        "<!-- page:1 -->\na\n<!-- page:2 -->\nb\n<!-- page:1 -->\na") == 2


@pytest.mark.skipif(fitz is None, reason="PyMuPDF 未安装")
def test_pdf_source_page_count_independent_of_parsed(monkeypatch):
    """B2（功能实证）：截页 PDF 的「已解析页数」与「源文件总页数」必须分离。

    把上限压到 2 页去解析 3 页 PDF：`page_count`（已解析）= 2，
    `source_page_count`（源文件真实总页数）= 3。引入前 diag 里没有
    source_page_count，下游只能拿到 2 ⇒ 覆盖率恒等 1.0。
    """
    monkeypatch.setattr(fp, "MAX_PDF_PAGES", 2)
    doc = fitz.open()
    # ⚠️ 每页必须写够文字量：平均每页字符数低于阈值会触发文档级 OCR 兜底，
    #    OCR 会把这 3 页全重新读一遍（本次测试要验证的是「截页后」「已解析
    #    页数」与「源文件总页数」分离，不能被兜底通道把页数补齐）。
    for i in range(3):
        page = doc.new_page()
        for j in range(4):
            page.insert_text(
                (72, 100 + j * 20),
                f"第{i + 1}页 基坑支护设计说明及土层参数与地下水位观测数据汇总表",
                fontname="china-s", fontsize=10)
    pdf_bytes = doc.tobytes()
    doc.close()

    _text, diag = fp.parse_file_content_ex(pdf_bytes, "超长.pdf")
    assert diag["page_count"] == 2, f"已解析页数应为 2，got {diag['page_count']}"
    assert diag["source_page_count"] == 3, \
        f"源文件总页数应为 3，got {diag.get('source_page_count')}"
    assert diag["truncated"] is True


def test_completeness_snapshot_separates_total_and_parsed():
    """B3：快照里 total_pages 与 parsed_pages 必须可分离，且缺省向后兼容。"""
    structured = {"page_count": 500, "table_count": 0, "image_count": 0,
                  "formula_count": 0}
    snap = doc_pipeline._completeness_snapshot(structured, [], None,
                                               source_page_count=600)
    assert snap["parsed_pages"] == 500
    assert snap["total_pages"] == 600
    # 未上报源页数（非 PDF / 旧调用点）→ 与已解析页数相同（旧行为逐字不变）
    snap2 = doc_pipeline._completeness_snapshot(structured, [], None)
    assert snap2["total_pages"] == snap2["parsed_pages"] == 500


async def test_build_completeness_report_coverage_below_one(db_conn):
    """B4：截页文档的 parse_coverage 必须 < 1（引入前恒等 1.0）。

    分母 max() 里的三项（page_count 列 / hint / parsed_pages）全是同源派生，
    引入前不可能小于分子。此处同时验证旧快照（无 total_pages 键）仍按旧行为
    算成 1.0 —— 保证存量库不因本次改动而改变既有结论。
    """
    doc_id = uuid.uuid4().hex
    pid = uuid.uuid4().hex
    snap_new = json.dumps({"parsed_pages": 500, "total_pages": 600})
    await db_conn.execute(
        "INSERT INTO project_documents"
        " (id, project_id, file_name, page_count, parsed_markdown,"
        "  parse_status, parse_warnings, completeness_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, pid, "超长方案.pdf", 500, "正文", "success", "[]", snap_new))
    await db_conn.commit()

    rep = await doc_pipeline.build_completeness_report(
        db_conn, doc_id=doc_id, project_id=pid)
    comp = rep["completeness"]
    assert comp["total_pages"] == 600
    assert comp["parsed_pages"] == 500
    assert comp["parse_coverage"] < 1.0
    assert comp["parse_coverage"] == pytest.approx(0.833, abs=1e-3), \
        f"500/600 覆盖率应约 0.833，got {comp['parse_coverage']}"

    # 旧快照（引入前写入，无 total_pages 键）→ 覆盖率仍为 1.0（向后兼容）
    old_id = uuid.uuid4().hex
    await db_conn.execute(
        "INSERT INTO project_documents"
        " (id, project_id, file_name, page_count, parsed_markdown,"
        "  parse_status, parse_warnings, completeness_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (old_id, pid, "旧文档.pdf", 12, "正文", "success", "[]",
         json.dumps({"parsed_pages": 12})))
    await db_conn.commit()
    old_rep = await doc_pipeline.build_completeness_report(
        db_conn, doc_id=old_id, project_id=pid)
    assert old_rep["completeness"]["parse_coverage"] == 1.0


def test_ingest_parsed_doc_forwards_source_page_count():
    """B5（源码锚）：router → pipeline 的透传不能断链。

    `_parse_pdf` 求出 source_page_count 后，必须经 `_ingest_parsed_doc`
    显式传给 `ingest_parse_result`，否则快照里永远只有退化值。
    """
    src = inspect.getsource(gf_docs._ingest_parsed_doc)
    assert "source_page_count=" in src, "_ingest_parsed_doc 未透传 source_page_count"
    sig = inspect.signature(doc_pipeline.ingest_parse_result)
    assert "source_page_count" in sig.parameters
    assert sig.parameters["source_page_count"].default == 0, \
        "新增参数必须默认 0（向后兼容）"


# ---------------------------------------------------------------------------
# C 组：批量重试口径 + 断点续跑诊断
# ---------------------------------------------------------------------------

async def test_checkpoint_table_has_diag_json(db_conn):
    """C1：检查点表必须有 diag_json 列（旧库由守卫式 ALTER 补列）。"""
    cur = await db_conn.execute("PRAGMA table_info(doc_parse_checkpoints)")
    cols = {r["name"] for r in await cur.fetchall()}
    assert "diag_json" in cols, f"doc_parse_checkpoints 缺 diag_json，实际列: {cols}"


async def test_checkpoint_diag_roundtrip(db_conn):
    """C2：diag 写入 → 读回 必须无损（中文不转义、键值一致）。"""
    doc_id = uuid.uuid4().hex
    diag = {"file_type": "pdf", "page_count": 500, "source_page_count": 600,
            "truncated": True,
            "warnings": ["PDF 共 600 页，仅解析前 500 页"]}
    await gf_docs._save_parse_checkpoint(
        db_conn, doc_id, "persist", "failed", "落库瞬时失败", "正文缓存",
        diag=diag)
    cp = await gf_docs._load_parse_checkpoint(db_conn, doc_id)
    assert cp is not None
    assert cp["stage"] == "persist" and cp["status"] == "failed"
    assert cp["text_cache"] == "正文缓存"
    assert json.loads(cp["diag_json"]) == diag


def test_restore_diag_from_checkpoint_full_and_legacy():
    """C3：续跑诊断恢复 —— 新检查点全量还原，旧检查点也不能回落成 1。

    引入前 resume 分支硬写 `{"resumed_from_stage", "warnings": []}`：
    page_count 回落 1（覆盖率/质量评分失真）、truncated 与告警全丢、
    file_type 丢失（parse_engine 写空）。
    """
    text = "<!-- page:1 -->\na\n<!-- page:2 -->\nb"
    # ① 新检查点：diag_json 完整还原
    cp = {"diag_json": json.dumps({
        "file_type": "pdf", "page_count": 2, "source_page_count": 9,
        "truncated": True, "warnings": ["PDF 共 9 页，仅解析前 2 页"]})}
    d = gf_docs._restore_diag_from_checkpoint(cp, "persist", text)
    assert d["resumed_from_stage"] == "persist"
    assert d["page_count"] == 2 and d["source_page_count"] == 9
    assert d["truncated"] is True
    assert d["file_type"] == "pdf"
    assert d["warnings"] == ["PDF 共 9 页，仅解析前 2 页"]

    # ② 旧检查点（无 diag_json）：页数按正文页标记现算，file_type 回落库列
    legacy = gf_docs._restore_diag_from_checkpoint(
        {"diag_json": ""}, "persist", text, file_type_fallback="PDF")
    assert legacy["page_count"] == 2, "旧检查点也必须从正文还原页数（不能回落 1）"
    assert legacy["file_type"] == "pdf"
    assert legacy["truncated"] is False
    assert legacy["warnings"] == []

    # ③ 脏数据不炸、不越权透传
    junk = gf_docs._restore_diag_from_checkpoint(
        {"diag_json": "not-a-json"}, "persist", text)
    assert junk["page_count"] == 2
    evil = gf_docs._restore_diag_from_checkpoint(
        {"diag_json": json.dumps({"__evil__": 1, "page_count": 3})},
        "persist", text)
    assert "__evil__" not in evil
    assert evil["page_count"] == 3


def test_batch_parse_uses_retry_outlet():
    """C4（源码锚）：批量解析必须与单份解析走同一重试出口。

    单份走 `_persist_parse_result_with_retry`（退避 2 次），批量直接调无重试的
    `_persist_parse_result` ⇒ 同一份文件「单个点能成、批量却失败」。
    """
    src = inspect.getsource(gf_docs.parse_all_documents)
    assert "_persist_parse_result_with_retry(" in src, \
        "批量解析未走重试出口（与单份解析口径分叉）"
    assert "_gf._persist_parse_result(" not in src, \
        "批量解析仍残留无重试的 _persist_parse_result 调用"


# ---------------------------------------------------------------------------
# D 组：上传响应 saved[] 字段集对称
# ---------------------------------------------------------------------------

def test_upload_saved_items_carry_type_and_category():
    """D1（源码锚 + 跨接口对称）：上传响应 saved[] 必须回类型与自动分类。

    旧实现 `saved.append({"id", "file_name", "size"})` —— 前端拿到「已保存」
    后想按文件类型渲染图标、或想展示自动分类结果，必须再拉一次 /documents；
    对接目录识别链路的调用方（不经列表接口）完全拿不到类型。
    本断言同时钉住「写侧 saved[]」与「读侧列表/预览」两侧字段名一致，
    防止后续任一侧改名而静默错位。
    """
    up_src = inspect.getsource(gf_docs.upload_documents)
    m = re.search(r"saved\.append\(\{(.*?)\}\)", up_src, re.S)
    assert m, "未能定位 upload_documents 的 saved.append 语句"
    body = m.group(1)
    assert '"file_type"' in body, "上传响应 saved[] 缺 file_type"
    assert '"doc_category"' in body, "上传响应 saved[] 缺 doc_category"
    # 既有三键不得被替换丢失（前端已按这几个键展示文件名/大小）
    for key in ('"id"', '"file_name"', '"size"'):
        assert key in body, f"上传响应 saved[] 丢失既有键 {key}"

    # 读侧同名字段：列表接口与预览接口都按这两列回，写侧改名即破坏对称
    list_src = inspect.getsource(gf_docs.list_documents)
    assert "file_type" in list_src and "doc_category" in list_src, \
        "列表接口字段集发生变化，saved[] 的对称性不再成立"
