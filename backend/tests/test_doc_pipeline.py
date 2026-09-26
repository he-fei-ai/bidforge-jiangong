"""文件上传解析结果四层存储 + 项目功能提取工作流的回归测试

覆盖《文件上传解析结果存储格式与项目功能提取工作流程规范》实施后的核心契约：
1. 原文层：MD5/SHA256 指纹、meta 落盘、documents_index.json、原件双份保存
2. 解析层：页标记、表格/图片/公式结构化（source_ref 可追溯）
3. 分块：四种 chunk_type、source_ref、块级 hash、前后链
4. 提取层物化：bid_analysis_items + global_facts → doc_extractions + extracted/*.json
5. 完整性报告 / 时效性判定（指纹变更、过期）/ 增量跳过
6. 既有上传/解析链路的挂钩（失败不影响主链路）
"""
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from starlette.datastructures import UploadFile

import app.routers.global_facts as gf
import app.routers.doc_pipeline as dp_api
import app.services.doc_pipeline.doc_storage as store
import app.services.doc_pipeline.pipeline as pipeline
from app.services.doc_pipeline.doc_chunker import chunk_document, SEMANTIC_CHUNK_SIZE
from app.services.doc_pipeline.md_structured import parse_markdown_structured


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


@pytest_asyncio.fixture
async def ctx(db_conn, tmp_path, monkeypatch):
    """重定向四层存储根目录与上传目录到 tmp，建一个项目。"""
    uploads = tmp_path / "uploads"
    docs_root = tmp_path / "projects"
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    monkeypatch.setattr(store, "DOCS_ROOT", docs_root)
    pid = uuid.uuid4().hex
    # 上传链路会校验项目作用域（_resolve_project_id），必须先建项目行
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.commit()
    return db_conn, pid, docs_root, uploads


# ---------------------------------------------------------------------------
# 1. 原文层：指纹 / meta / 索引 / 原子写
# ---------------------------------------------------------------------------

def test_fingerprints_and_atomic_io(tmp_path):
    data = "深度 12.5m".encode("utf-8")
    fp = store.file_fingerprints(data)
    assert len(fp["md5"]) == 32 and len(fp["sha256"]) == 64
    dest = tmp_path / "a" / "b.json"
    store.atomic_write_json(dest, {"k": "中文"})
    assert store.read_json(dest) == {"k": "中文"}
    assert store.read_json(tmp_path / "不存在.json") is None
    assert store.hashes_differ(fp, {"md5": fp["md5"]}) is False
    assert store.hashes_differ(fp, None) is True


async def test_ingest_upload_writes_raw_layer(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = uuid.uuid4().hex
    f = _upload("设计说明.txt", "第一章 工程概况\n\n基坑深度 12.5m。".encode("utf-8"))
    res = await gf.upload_documents(scheme_id="", project_id=pid, files=[f], db=db)
    assert res["saved_count"] == 1
    saved_path = uploads / pid / f"{doc_id[:8]}" if False else None  # 占位防误读
    doc_id = res["saved"][0]["id"]

    # DB 指纹列已写入（时效性）
    cur = await db.execute(
        "SELECT file_hash_md5, file_hash_sha256, parse_status"
        " FROM project_documents WHERE id=?", (doc_id,))
    row = dict(await cur.fetchone())
    assert len(row["file_hash_md5"]) == 32 and len(row["file_hash_sha256"]) == 64
    assert row["parse_status"] == "pending"

    # 磁盘：raw 原件 + parsed meta + 索引
    meta = store.read_meta(pid, doc_id)
    assert meta and meta["file_hash_md5"] == row["file_hash_md5"]
    raw_dir = store.layer_dir(pid, doc_id, store.LAYER_RAW)
    originals = list(raw_dir.glob(f"{doc_id}_original*"))
    assert originals, "原件必须双份保存到 raw 层"
    idx = store.read_json(store.index_path(pid))
    assert isinstance(idx, list) and idx[0]["doc_id"] == doc_id


# ---------------------------------------------------------------------------
# 2. 解析层结构化抽取
# ---------------------------------------------------------------------------

MD_SAMPLE = (
    "<!-- page:1 -->\n"
    "# 第一章 工程概况\n\n"
    "## 1.1 项目基本信息\n\n"
    "- 项目名称：某某深基坑工程\n\n"
    "工程规模表\n"
    "【表格】\n"
    "| 项目 | 数值 | 单位 |\n"
    "| --- | --- | --- |\n"
    "| 基坑深度 | 12.5 | m |\n"
    "| 周长 | 380 | m |\n\n"
    "<!-- page:2 -->\n"
    "# 第二章 支护设计\n\n"
    "采用 SMW 工法桩。\n\n"
    "![支护剖面图](images/img1.png)\n"
)


def test_parse_markdown_structured_pages_tables():
    out = parse_markdown_structured(MD_SAMPLE, doc_id="dX")
    assert out["page_count"] == 2
    assert out["table_count"] == 1
    t = out["tables"][0]
    assert t["headers"] == ["项目", "数值", "单位"]
    assert t["rows"] == [["基坑深度", "12.5", "m"], ["周长", "380", "m"]]
    assert t["page_num"] == 1
    assert t["source_ref"] == "dX#page:1#table:t1"
    assert t["title"] == "工程规模表"
    # 页 2 覆盖表格与图片
    assert out["pages"][1]["images"] == ["img_001"]
    assert out["images"][0]["source_ref"] == "dX#page:2#img:img_001"
    # 表格行不重复进入页文本，但留结构化占位
    assert "基坑深度 | 12.5" not in out["pages"][0]["text"]
    assert "表格见 tables" in out["pages"][0]["text"]


def test_chunker_section_table_semantic():
    chunks = chunk_document(MD_SAMPLE, doc_id="dX",
                            structured=parse_markdown_structured(
                                MD_SAMPLE, doc_id="dX"))
    types = {c["chunk_type"] for c in chunks}
    assert "table" in types and "section" in types
    for c in chunks:
        assert c["source_ref"].startswith("dX#page:"), "每块必须可溯源"
        assert c["hash"]
    tbl = next(c for c in chunks if c["chunk_type"] == "table")
    assert tbl["tables"] == ["t1"] and "基坑深度" in tbl["text"]
    # 前后链完整
    assert chunks[0]["prev_chunk_id"] is None
    assert chunks[0]["next_chunk_id"] == chunks[1]["chunk_id"]
    # 超长章节切语义块（重叠保持内容连续）
    long_md = "# 长章节\n\n" + ("内容文字。" * 400)
    lc = chunk_document(long_md, doc_id="dL")
    sem = [c for c in lc if c["chunk_type"] == "semantic"]
    assert sem and all(len(c["text"]) <= SEMANTIC_CHUNK_SIZE for c in sem)


# ---------------------------------------------------------------------------
# 3. 解析入库（阶段2+3 落盘 + 增量 diff）
# ---------------------------------------------------------------------------

async def _upload_and_parse(ctx, content: bytes = None):
    db, pid, docs_root, uploads = ctx
    content = content or "# 第一章 工程概况\n\n基坑深度 12.5m。\n".encode("utf-8")
    f = _upload("设计说明.txt", content)
    res = await gf.upload_documents(scheme_id="", project_id=pid, files=[f], db=db)
    doc_id = res["saved"][0]["id"]
    parsed = await gf.parse_document(doc_id, force=False, db=db)
    assert parsed["ok"]
    return doc_id


async def test_parse_populates_four_layers(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)

    # DB 时效列
    cur = await db.execute(
        "SELECT parse_status, parse_version, file_hash_md5, page_count,"
        " completeness_json, parsed_at FROM project_documents WHERE id=?", (doc_id,))
    row = dict(await cur.fetchone())
    assert row["parse_status"] == "success"
    assert row["parse_version"] == "v1"
    assert len(row["file_hash_md5"]) == 32
    snap = json.loads(row["completeness_json"])
    assert snap["chunk_count"] >= 1

    # 解析层磁盘产物齐全
    pdir = store.layer_dir(pid, doc_id, store.LAYER_PARSED)
    for suffix in ("_parsed.md", "_pages.json", "_tables.json",
                   "_images.json", "_meta.json"):
        assert (pdir / f"{doc_id}{suffix}").exists(), f"缺解析层产物 {suffix}"
    md = (pdir / f"{doc_id}_parsed.md").read_text(encoding="utf-8")
    assert md.startswith("---\ndoc_id:"), "解析层 Markdown 需 front-matter"

    # 语义层索引
    sem = store.read_json(
        store.layer_dir(pid, doc_id, store.LAYER_SEMANTIC) / f"{doc_id}_semantic.json")
    assert sem["status"] == "index_ready" and sem["chunk_count"] >= 1

    # doc_chunks 溯源
    cur = await db.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN source_ref LIKE ? THEN 1 ELSE 0 END) t"
        " FROM doc_chunks WHERE doc_id=?", (f"{doc_id}#page:%", doc_id))
    r = dict(await cur.fetchone())
    assert r["n"] >= 1 and r["n"] == r["t"], "每块都要有 source_ref"

    # 重复解析：版本递增到 v2（时效性）
    await gf.parse_document(doc_id, force=True, db=db)
    cur = await db.execute(
        "SELECT parse_version FROM project_documents WHERE id=?", (doc_id,))
    assert (await cur.fetchone())["parse_version"] == "v2"


async def test_incremental_reparse_skips_unchanged(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    # 指纹未变 → reparse 增量模式跳过
    res = await dp_api.reparse_document(doc_id, body={"mode": "incremental"}, db=db)
    assert res["ok"] and res["skipped"] is True

    # 源文件变更 → 指纹不一致，触发重解析
    fpath = uploads / pid / [p.name for p in (uploads / pid).iterdir()
                             if p.name.startswith(doc_id[:8])][0]
    fpath.write_bytes("# 第二章 新内容\n\n支模高度 8.5m。\n".encode("utf-8"))
    res2 = await dp_api.reparse_document(doc_id, body={"mode": "incremental"}, db=db)
    assert res2.get("skipped") in (False, None)
    assert res2["file_changed_detected"] is True
    cur = await db.execute(
        "SELECT parse_version FROM project_documents WHERE id=?", (doc_id,))
    assert (await cur.fetchone())["parse_version"] == "v2"


# ---------------------------------------------------------------------------
# 4. 提取层物化（阶段4 产物 → 标准格式，阶段7 入库索引）
# ---------------------------------------------------------------------------

async def test_sync_extract_layer(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    # 模拟阶段4 AI 提取产物：解析项 + 全局事实
    await db.execute(
        "INSERT INTO bid_analysis_items (id, project_id, item_id, label,"
        " output_type, required, status, content, sort_order)"
        " VALUES (?,?,?,?,'json',1,'success',?,1)",
        (f"{pid}_projectBasicInfo", pid, "projectBasicInfo", "项目级基本信息",
         json.dumps({"project_info": {
             "project_name": {"value": "某某工程", "confidence": 0.98,
                              "source": f"{doc_id}#page:1#para:3"},
             "location": {"value": "上海市嘉定区"}}}, ensure_ascii=False)))
    await db.execute(
        "INSERT INTO global_facts (id, project_id, group_id, group_title, title,"
        " content, category, source_ref, confidence, is_resolved)"
        " VALUES (?,?,?,?,?,?,?,?,?,1)",
        (uuid.uuid4().hex, pid, "project_team", "项目角色变量", "项目经理",
         "- **项目经理**: 张伟", "personnel",
         json.dumps([{"file": "设计说明.txt", "quote": "page:8#para:3"}]), 0.9))
    await db.commit()

    res = await dp_api.sync_extractions(doc_id, db=db)
    assert res["extract_status"] == "success"
    assert "project_info" in res["written_types"]
    assert "global_facts" in res["written_types"]

    # 磁盘提取层 + DB 双份
    payload = store.read_extraction(pid, doc_id, "project_info")
    assert payload["project_info"]["project_name"]["value"] == "某某工程"
    cur = await db.execute(
        "SELECT extract_type, source_refs, confidence FROM doc_extractions"
        " WHERE doc_id=? ORDER BY extract_type", (doc_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    assert {r["extract_type"] for r in rows} >= {"project_info", "global_facts"}
    refs = json.loads(rows[0]["source_refs"])
    assert any("page:" in x for x in refs), "提取结果必须保留溯源引用"

    # 查询端点（幂等重跑 = 人工校正后刷新）
    q = await dp_api.get_extractions(doc_id, type="project_info", db=db)
    assert q["items"][0]["extract_data"]["project_info"]["project_name"]["value"]
    res2 = await dp_api.sync_extractions(doc_id, db=db)
    assert res2["extract_status"] == "success"


# ---------------------------------------------------------------------------
# 5. 完整性 / 时效性 / 交叉校验
# ---------------------------------------------------------------------------

async def test_completeness_report(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    rep = await dp_api.document_completeness(doc_id, refresh=True, db=db)
    c = rep["completeness"]
    assert c["parse_coverage"] == 1.0
    assert c["chunk_count"] >= 1
    assert 0 <= rep["quality_score"] <= 1
    # 报告已归档（可追溯）
    cur = await db.execute(
        "SELECT COUNT(*) n FROM doc_validation_reports WHERE doc_id=? AND kind"
        "='completeness'", (doc_id,))
    assert ((await cur.fetchone())["n"]) >= 1


async def test_freshness_states(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    fresh = await dp_api.document_freshness(doc_id, db=db)
    assert fresh["status"] == "valid"

    # 篡改源文件 → file_changed
    fpath = uploads / pid / [p.name for p in (uploads / pid).iterdir()
                             if p.name.startswith(doc_id[:8])][0]
    fpath.write_bytes("改过的内容，完全不同。".encode("utf-8"))
    fresh2 = await dp_api.document_freshness(doc_id, db=db)
    assert fresh2["status"] == "file_changed"

    # 过期 → expired
    meta = store.read_meta(pid, doc_id)
    meta["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)
                          ).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["file_hash_md5"] = fresh["file_hash_md5"]  # 排除变更干扰，只验过期
    store.write_meta(pid, doc_id, meta)
    cur = await db.execute("SELECT file_hash_md5 FROM project_documents WHERE id=?",
                           (doc_id,))
    unchanged_md5 = meta["file_hash_md5"]
    fresh3 = pipeline.compute_freshness(meta, current_md5=unchanged_md5)
    assert fresh3["status"] == "expired"


async def test_cross_check_persists(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    await db.execute(
        "INSERT INTO global_facts (id, project_id, title, content, category,"
        " fact_key, has_conflict) VALUES (?,?,?,?,?,?,1)",
        (uuid.uuid4().hex, pid, "总工期", "- **总工期**: 450天", "schedule",
         "total_duration"))
    await db.commit()
    rep = await dp_api.cross_check(doc_id, db=db)
    assert rep["fact_count"] == 1
    assert rep["stored_conflicts"] == 1
    cur = await db.execute(
        "SELECT COUNT(*) n FROM doc_validation_reports WHERE kind='cross_check'")
    assert ((await cur.fetchone())["n"]) == 1


# ---------------------------------------------------------------------------
# 6. 状态查询 / 删除清理 / 失败降级
# ---------------------------------------------------------------------------

async def test_status_and_index_endpoints(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    st = await dp_api.document_status(doc_id, db=db)
    assert st["parse_status"] == "success"
    assert set(st["layers"]) == {"raw", "parsed", "extracted", "semantic"}
    assert st["layers"]["parsed"], "解析层产物应可见"
    ix = await dp_api.project_documents_index(pid, db=db)
    assert ix["count"] == 1 and ix["documents"][0]["layers_on_disk"]


async def test_delete_purges_layers(ctx):
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    assert store.doc_dir(pid, doc_id).exists()
    await gf.delete_document(doc_id, db=db)
    assert not store.doc_dir(pid, doc_id).exists(), "四层目录须随文档删除清理"
    cur = await db.execute("SELECT COUNT(*) n FROM doc_chunks WHERE doc_id=?",
                           (doc_id,))
    assert ((await cur.fetchone())["n"]) == 0


async def test_layer_failure_does_not_break_parse(ctx, monkeypatch):
    """磁盘产物写入失败（如目录不可写）→ 解析主链路仍成功，DB 状态正确。

    真实语义下 `_safe_io` 会捕获底层写盘异常并降级告警（返回 None），DB 事务
    照常提交。故此处让 store 的磁盘写函数抛 OSError，而非替换 `_safe_io` 本身。
    """
    db, pid, docs_root, uploads = ctx

    def _boom(*a, **k):
        raise OSError("disk full")
    for fn in ("write_parsed_layer", "write_meta", "write_semantic_index",
               "write_extraction", "update_index"):
        monkeypatch.setattr(store, fn, _boom)

    doc_id = await _upload_and_parse(ctx)
    cur = await db.execute(
        "SELECT parse_status, parsed_markdown FROM project_documents WHERE id=?",
        (doc_id,))
    row = dict(await cur.fetchone())
    assert row["parse_status"] == "success" and row["parsed_markdown"]
    # 块仍然入库（DB 层不受磁盘故障影响）
    cur = await db.execute("SELECT COUNT(*) n FROM doc_chunks WHERE doc_id=?",
                           (doc_id,))
    assert ((await cur.fetchone())["n"]) >= 1


# ---------------------------------------------------------------------------
# 7. 2026-09-20 审计回归：同名替换泄漏 / 解析代次回退 / 分类三侧口径
# ---------------------------------------------------------------------------

async def test_same_name_reupload_purges_old_layers(ctx):
    """同名替换上传：旧文档的 doc_chunks 行与四层磁盘产物必须同步清理。

    旧实现只删 project_documents 行 —— 旧块/旧层目录永久残留，
    与「手动删除文档即 purge_document」口径不一致（孤儿泄漏）。
    """
    db, pid, docs_root, uploads = ctx
    first = await _upload_and_parse(ctx)
    assert store.doc_dir(pid, first).exists()
    cur = await db.execute(
        "SELECT COUNT(*) n FROM doc_chunks WHERE doc_id=?", (first,))
    assert ((await cur.fetchone())["n"]) >= 1

    # 同名再传（内容不同→指纹/原件都会替换）
    f = _upload("设计说明.txt", "# 新版\n\n新内容说明。\n".encode("utf-8"))
    res = await gf.upload_documents(scheme_id="", project_id=pid, files=[f], db=db)
    assert res["replaced"] == 1
    new_id = res["saved"][0]["id"]
    assert new_id != first

    cur = await db.execute("SELECT COUNT(*) n FROM doc_chunks WHERE doc_id=?",
                           (first,))
    assert ((await cur.fetchone())["n"]) == 0, "旧文档分块必须随替换清理"
    assert not store.doc_dir(pid, first).exists(), "旧文档四层目录必须随替换清理"
    # 新文档尚未解析：不应残留上一代的目录


async def test_reparse_legacy_empty_version_bumps_to_v2(ctx):
    """旧文档（parse_version 列为空）重解析：代次视作 v1→v2，不得回退重置为 v1。"""
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    # 模拟四层存储上线前解析的存量文档：有内容、无版本记录
    await db.execute("UPDATE project_documents SET parse_version='' WHERE id=?",
                     (doc_id,))
    await db.commit()
    parsed = await gf.parse_document(doc_id, force=True, db=db)
    assert parsed["ok"]
    cur = await db.execute(
        "SELECT parse_version FROM project_documents WHERE id=?", (doc_id,))
    assert (await cur.fetchone())["parse_version"] == "v2"


async def test_category_update_rejects_unknown_value(ctx):
    """分类改判只接受登记过的合法值（三侧口径：提取优先级/Tag 颜色都依赖它）。"""
    import fastapi
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    with pytest.raises(fastapi.HTTPException) as ei:
        await gf.update_document_category(doc_id, {"doc_category": "随便写的"}, db=db)
    assert ei.value.status_code == 400
    cur = await db.execute(
        "SELECT doc_category FROM project_documents WHERE id=?", (doc_id,))
    assert (await cur.fetchone())["doc_category"] != "随便写的"
    # 合法值仍可通过
    ok = await gf.update_document_category(doc_id, {"doc_category": "地勘报告"}, db=db)
    assert ok["ok"] is True


async def test_empty_reason_buckets_contract(ctx):
    """上传拒绝原因五类桶契约：unsupported/signature_invalid 回传键稳定，
    前端拒绝文案拼接依赖这些字段名（三侧口径：后端回传键/前端消费/登记）。"""
    db, pid, docs_root, uploads = ctx
    f = _upload("report.exe", b"MZ\x90\x00 not a document at all")
    res = await gf.upload_documents(scheme_id="", project_id=pid, files=[f], db=db)
    assert res["saved_count"] == 0
    assert res.get("unsupported") == ["report.exe"]
    cur = await db.execute(
        "SELECT COUNT(*) n FROM project_documents WHERE project_id=?", (pid,))
    assert ((await cur.fetchone())["n"]) == 0


# ---------------------------------------------------------------------------
# 8. 2026-09-21 上传解析模块专项审计：质量分回写 / 状态 0 分 / 分块分页过滤
# ---------------------------------------------------------------------------

async def test_completeness_persists_quality_to_db_column(ctx):
    """完整性校验必须把质量分回写 project_documents.quality_score（status 读该列）。

    旧实现只写磁盘 meta，从不回写 DB 列（默认 -1 哨兵）—— 于是 /status 的
    quality_score 恒为 null，四层质量能力对用户不可见。
    """
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    # 初始：列仍为默认哨兵（-1，未评估）
    cur = await db.execute(
        "SELECT quality_score FROM project_documents WHERE id=?", (doc_id,))
    assert float((await cur.fetchone())["quality_score"]) < 0

    rep = await dp_api.document_completeness(doc_id, refresh=True, db=db)
    q = rep["quality_score"]
    cur = await db.execute(
        "SELECT quality_score FROM project_documents WHERE id=?", (doc_id,))
    assert float((await cur.fetchone())["quality_score"]) == pytest.approx(q)
    # 状态接口现在能读出真实质量分（不再恒 null）
    st = await dp_api.document_status(doc_id, db=db)
    assert st["quality_score"] is not None


async def test_status_quality_score_zero_not_swallowed(ctx):
    """质量分恰为 0 不得被 `or -1` 短路吞成 null；仅 -1 哨兵 / None 才归一 null。"""
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)
    await db.execute(
        "UPDATE project_documents SET quality_score=0 WHERE id=?", (doc_id,))
    await db.commit()
    st = await dp_api.document_status(doc_id, db=db)
    assert st["quality_score"] == 0
    # -1 哨兵（未评估）仍应为 null
    await db.execute(
        "UPDATE project_documents SET quality_score=-1 WHERE id=?", (doc_id,))
    await db.commit()
    st2 = await dp_api.document_status(doc_id, db=db)
    assert st2["quality_score"] is None


async def test_chunks_total_respects_type_filter(ctx):
    """分块查询 total 必须与 chunk_type 过滤口径一致（旧实现恒为全量→分页虚高）。"""
    db, pid, docs_root, uploads = ctx
    content = (
        "# 第一章 工程概况\n\n这里是正文段落，长度足够通过校验。\n\n"
        "工程规模表\n【表格】\n| 项目 | 数值 |\n| --- | --- |\n| 深度 | 12.5 |\n"
    ).encode("utf-8")
    doc_id = await _upload_and_parse(ctx, content=content)
    all_res = await dp_api.get_chunks(
        doc_id, chunk_type="", page_num=0, limit=500, offset=0, db=db)
    tbl_res = await dp_api.get_chunks(
        doc_id, chunk_type="table", page_num=0, limit=500, offset=0, db=db)
    assert tbl_res["total"] >= 1
    assert all_res["total"] > tbl_res["total"], "全量 total 应大于按类型过滤后"
    assert tbl_res["total"] == len(tbl_res["items"]), "total 必须等于过滤后命中数"


# ---------------------------------------------------------------------------
# 2026-09-25 技术债修复回归：created_at 继承 / 提取类别覆盖护栏 /
# completeness 响应结构 / 置信度未评估哨兵
# ---------------------------------------------------------------------------

class TestExtractTypeCoverage:
    """提取类别 ↔ 解析项映射的漂移护栏（boq 预留契约的机器可校验形式）。"""

    def test_every_active_extract_type_has_mapping(self):
        # 非预留类别必须至少被一个解析项物化（global_facts 由全局事实表特供，
        # 在 sync_extract_layer 中单独分支处理，不依赖解析项映射），防止新增
        # 类别时静默漏接
        mapped = set()
        for types in pipeline._ITEM_TO_EXTRACT_TYPE.values():
            mapped.update(types)
        special_sources = {"global_facts"}
        active = [t for t in store.EXTRACT_TYPES
                  if t not in store.RESERVED_EXTRACT_TYPES and t not in special_sources]
        missing = [t for t in active if t not in mapped]
        assert not missing, f"非预留提取类别缺少解析项映射: {missing}"

    def test_reserved_types_have_no_mapping(self):
        # 预留类别（boq）必须保持「无映射」状态：一旦接入需显式摘除预留登记
        mapped = set()
        for types in pipeline._ITEM_TO_EXTRACT_TYPE.values():
            mapped.update(types)
        for t in store.RESERVED_EXTRACT_TYPES:
            assert t not in mapped, f"预留类别 {t} 已有映射，应从 RESERVED_EXTRACT_TYPES 摘除"


async def test_reparse_preserves_created_at_of_unchanged_chunks(ctx, monkeypatch):
    """✅ 2026-09-25：内容未变更的块在重解析后必须继承旧 created_at
    （时间线连续，增量审计可区分新旧块）；内容变更的块刷新为重解析时刻
    （用固定时间戳避免秒级粒度的同秒抖动）。"""
    db, pid, docs_root, uploads = ctx
    content = "# 第一章 工程概况\n\n基坑深度 12.5m，地质稳定。\n".encode("utf-8")
    doc_id = await _upload_and_parse(ctx, content=content)

    async def _chunk_times():
        cur = await db.execute(
            "SELECT chunk_id, hash, created_at FROM doc_chunks WHERE doc_id=?"
            " ORDER BY chunk_id", (doc_id,))
        return {r["chunk_id"]: (r["hash"], r["created_at"])
                for r in await cur.fetchall()}

    first = await _chunk_times()
    assert first, "首次解析必须产出分块"

    # 内容未变：force 重解析（绕过指纹跳过），未变更块 created_at 不应被刷新
    await gf.parse_document(doc_id, force=True, db=db)
    second = await _chunk_times()
    assert set(second) == set(first), "确定性 chunk_id 应保持稳定"
    for cid, (h1, t1) in first.items():
        h2, t2 = second[cid]
        assert h2 == h1 and t2 == t1, f"未变更块 {cid} 的 created_at 被重解析刷新"

    # 内容变更：固定重解析时刻 → 变更块的 created_at 必须等于该时刻
    FROZEN = "2099-01-01T00:00:00Z"
    monkeypatch.setattr(pipeline, "_now", lambda: FROZEN)
    fpath = uploads / pid / [p.name for p in (uploads / pid).iterdir()
                             if p.name.startswith(doc_id[:8])][0]
    fpath.write_bytes("# 第一章 工程概况\n\n基坑深度 15.0m，地质稳定。\n".encode("utf-8"))
    await dp_api.reparse_document(doc_id, body={"mode": "incremental"}, db=db)
    monkeypatch.undo()
    third = await _chunk_times()
    refreshed = [cid for cid, (h, t) in third.items()
                 if cid in first and t == FROZEN and h != first[cid][0]]
    assert refreshed, "内容变更后至少一个块的 created_at 应刷新为重解析时刻"


async def test_completeness_response_shape_consistent(ctx):
    """cached_at 两分支结构一致：实时计算为 None，缓存命中为落库时刻；
    conflict_count 附带 project 作用域标注。"""
    db, pid, docs_root, uploads = ctx
    doc_id = await _upload_and_parse(ctx)

    fresh = await dp_api.document_completeness(doc_id, refresh=True, db=db)
    assert fresh["cached_at"] is None, "实时计算分支 cached_at 应为 None"
    assert fresh["generated_at"], "实时计算必须携带 generated_at"
    assert fresh["completeness"]["conflict_scope"] == "project"

    cached = await dp_api.document_completeness(doc_id, refresh=False, db=db)
    assert cached["cached_at"], "缓存命中分支 cached_at 应为落库时刻"
    assert cached["completeness"]["conflict_scope"] == "project"


def test_avg_confidence_unassessed_sentinel():
    """无置信度字段时返回 0.0（未评估），不再伪装成 0.9 高确信。"""
    payload_md = {"doc_id": "d", "extract_time": "", "extract_schema_version": "v",
                  "engineering": {"overviewParams_markdown": "正文"},
                  "source_items": []}
    assert pipeline._avg_confidence(payload_md) == 0.0
    payload_json = {"doc_id": "d", "extract_time": "", "extract_schema_version": "v",
                    "project_info": {
                        "project_name": {"value": "x", "confidence": 0.98,
                                         "source": "s"}}}
    assert pipeline._avg_confidence(payload_json) == 0.98
    # Markdown 来源的 project_info 字段：三要素 confidence 为未评估哨兵
    merged = pipeline._merge_item_contents(
        "project_info", [{"item_id": "projectBasicInfo", "output_type": "markdown",
                          "content": "项目名称：某工程"}])
    field = merged["projectBasicInfo_markdown"]
    assert field["confidence"] == 0.0 and field["value"] == "项目名称：某工程"

