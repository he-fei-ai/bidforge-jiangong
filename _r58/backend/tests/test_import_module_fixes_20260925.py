"""解析提取（import）模块 · 2026-09-25 缺陷修复回归。

覆盖本轮定位到的 4 类真实缺陷：

1. **parse_status 陈旧不自修复**（"解析结果丢失 / 批量状态错乱"）
   四层存储上线前的存量行正文非空但 ``parse_status='pending'``（补列默认值），
   单份/批量解析的"已解析"短路分支只回 already_parsed / continue，从不修状态列。
2. **文档分类口径三处漂移**：分类清单（global_facts）、提取优先级
   （bid_analysis 内联 CATEGORY_PRIORITY，只覆盖 6/10 类）、前端配色表。
3. **分类错误**：「设计文件」含过宽「设计」关键词 → 施工组织设计被误归类。
4. **批量截断口径不一致**：单份判定 `char_truncated or diag["truncated"]`，
   批量多加 `and diag["warnings"]` → 解析器截断但无告警文本时静默漏报。

运行：python -m pytest tests/test_import_module_fixes_20260925.py -q
"""
import io
import sqlite3
import uuid

import pytest
from app.db import get_conn, init_db
from app.routers import bid_analysis as ba
from app.routers import global_facts as gf
from app.services import doc_categories as dc
from starlette.datastructures import UploadFile


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb = __import__("app.db", fromlist=["DB_PATH"])
    _appdb.DB_PATH = tmp_path / "import-fixes.sqlite"
    await init_db()
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)

    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await _appdb.close_db()


async def _insert_legacy_doc(db, pid, *, body="已有正文内容超过十个字符",
                             status="pending"):
    """插入一条模拟"四层存储上线前解析完成"的存量行（正文 + 陈旧状态）。"""
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents(id, project_id, file_name, file_type,"
        " parsed_markdown, parse_status) VALUES(?,?,?,?,?,?)",
        (doc_id, pid, f"legacy-{doc_id[:6]}.txt", "txt", body, status))
    await db.commit()
    return doc_id


async def _status(db, doc_id):
    cur = await db.execute(
        "SELECT parse_status, parsed_markdown FROM project_documents WHERE id=?",
        (doc_id,))
    row = await cur.fetchone()
    return row["parse_status"], row["parsed_markdown"]


# ---------------------------------------------------------------------------
# 1. parse_status 自修复
# ---------------------------------------------------------------------------

async def test_parse_document_reconciles_stale_status(ctx):
    """单份解析短路命中时必须把陈旧的 parse_status 修正为 success。"""
    db, pid, _sid = ctx
    doc_id = await _insert_legacy_doc(db, pid)
    assert (await _status(db, doc_id))[0] == "pending"  # 前置：确实陈旧

    res = await gf.parse_document(doc_id, db=db)

    assert res["ok"] and res["already_parsed"] is True
    assert res["parse_status"] == "success"
    assert res["reconciled"] is True
    assert (await _status(db, doc_id))[0] == "success"


async def test_parse_document_reconcile_is_idempotent(ctx):
    """已为 success 的行再次解析不应重复写、也不应回传 reconciled。"""
    db, pid, _sid = ctx
    doc_id = await _insert_legacy_doc(db, pid, status="success")

    res = await gf.parse_document(doc_id, db=db)

    assert res["already_parsed"] is True
    assert "reconciled" not in res
    assert (await _status(db, doc_id))[0] == "success"


async def test_reconcile_skips_pending_row_without_body(ctx):
    """反例：正文为空的 pending 行不得被误标 success（防"假解析成功"）。"""
    db, pid, _sid = ctx
    doc_id = await _insert_legacy_doc(db, pid, body="", status="pending")

    changed = await gf._reconcile_parse_status(db, doc_id)

    assert changed is False
    assert (await _status(db, doc_id))[0] == "pending"


async def test_parse_document_does_not_heal_when_body_short(ctx):
    """反例：正文不足有效长度时走真实解析失败路径，不得落 success。"""
    db, pid, sid = ctx
    # 上传一个真实存在的、内容过短的文件 → 解析被判"无有效文本"
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("tiny.txt", b"#")], db=db)
    cur = await db.execute(
        "SELECT id FROM project_documents WHERE project_id=?", (pid,))
    doc_id = (await cur.fetchone())["id"]

    with pytest.raises(Exception):
        await gf.parse_document(doc_id, force=True, db=db)

    status, _ = await _status(db, doc_id)
    assert status == "failed"


async def test_reconcile_keeps_failed_marker_when_body_empty(ctx):
    """反例：parse_status='failed' 且正文为空 → 不得被误修成 success。"""
    db, pid, _sid = ctx
    doc_id = await _insert_legacy_doc(db, pid, body="", status="failed")

    changed = await gf._reconcile_parse_status(db, doc_id)

    assert changed is False
    assert (await _status(db, doc_id))[0] == "failed"


async def test_reconcile_all_only_touches_stale_rows(ctx):
    """项目级自修复只动"正文非空 + 状态非 success"的行。"""
    db, pid, _sid = ctx
    stale1 = await _insert_legacy_doc(db, pid)                  # pending + 有正文
    stale2 = await _insert_legacy_doc(db, pid, status="error")  # error + 有正文
    ok_doc = await _insert_legacy_doc(db, pid, status="success")
    empty = await _insert_legacy_doc(db, pid, body="", status="failed")

    n = await gf._reconcile_parse_status_all(db, pid)

    assert n == 2
    assert (await _status(db, stale1))[0] == "success"
    assert (await _status(db, stale2))[0] == "success"
    assert (await _status(db, ok_doc))[0] == "success"
    assert (await _status(db, empty))[0] == "failed"  # 未被误改


# ---------------------------------------------------------------------------
# 2. parse-all 自修复（含"所有文档均已解析"早退分支）
# ---------------------------------------------------------------------------

async def test_parse_all_reconciles_on_early_return(ctx):
    """存量文档全部已解析但状态陈旧 → 早退分支也要修正状态。"""
    db, pid, sid = ctx
    await _insert_legacy_doc(db, pid)
    await _insert_legacy_doc(db, pid)

    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)

    assert res["parsed"] == 0
    assert res["reconciled_count"] == 2
    assert "修正 2 份" in res["message"]
    cur = await db.execute(
        "SELECT parse_status, COUNT(*) AS n FROM project_documents "
        "WHERE project_id=? GROUP BY parse_status", (pid,))
    rows = {r["parse_status"]: r["n"] for r in await cur.fetchall()}
    assert rows == {"success": 2}


async def test_parse_all_reconciles_and_parses_rest(ctx):
    """混合场景：1 份存量陈旧 + 1 份真待解析 → 修正与解析各得其所。"""
    db, pid, sid = ctx
    await _insert_legacy_doc(db, pid)
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("new.txt", "这是一份新上传的待解析文档内容。".encode("utf-8"))],
        db=db)

    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)

    assert res["parsed"] == 1
    assert res["failed_count"] == 0
    assert res["reconciled_count"] == 1
    cur = await db.execute(
        "SELECT parse_status, COUNT(*) AS n FROM project_documents "
        "WHERE project_id=? GROUP BY parse_status", (pid,))
    rows = {r["parse_status"]: r["n"] for r in await cur.fetchall()}
    assert rows == {"success": 2}


async def test_parse_all_reconciled_count_zero_when_clean(ctx):
    """反例：状态本就正确时 reconciled_count 必须为 0（不谎报修正）。"""
    db, pid, sid = ctx
    await _insert_legacy_doc(db, pid, status="success")

    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)

    assert res["reconciled_count"] == 0
    assert res["reconciled"] == []


async def test_parse_all_reports_parser_truncation_without_warnings(ctx, monkeypatch):
    """BUG 回归：解析器截断但 warnings 为空时，批量解析不得静默漏报。"""
    db, pid, sid = ctx
    # 上传真实文件（通过文件头校验），再打桩解析器返回"截断但无告警"
    await gf.upload_documents(
        scheme_id=sid, project_id="",
        files=[_upload("big.pdf", b"%PDF-1.4 fake body padding bytes")], db=db)

    def _fake_parse(content, filename):
        assert filename == "big.pdf"
        return ("正文内容足够十个字符以上" * 5,
                {"file_type": "pdf", "truncated": True, "warnings": [],
                 "page_count": 99})

    monkeypatch.setattr(gf, "parse_file_content_ex", _fake_parse)

    res = await gf.parse_all_documents(scheme_id=sid, project_id="", db=db)

    assert res["parsed"] == 1
    assert res["failed_count"] == 0
    assert res["truncated_count"] == 1, (
        "解析器标记 truncated 但 warnings 为空时必须计入截断（旧实现漏报）")
    assert "big.pdf" in [t["file_name"] for t in res["truncated"]]


# ---------------------------------------------------------------------------
# 3. 文档分类：单一事实源 + 分类精度
# ---------------------------------------------------------------------------

def test_category_options_covers_all_categories():
    """下拉选项必须覆盖全部分类且含兜底「其他」（顺序与规则表一致）。"""
    opts = dc.category_options()
    assert opts[:9] == list(dc.DOC_CATEGORIES)
    assert opts[-1] == "其他"
    for cat, _ in dc.AUTO_CLASSIFY_RULES:
        assert cat in opts


def test_autoclassify_rules_align_with_category_list():
    """护栏：规则表中出现的分类必须都在分类清单内（防漂移）。"""
    for cat, _kws in dc.AUTO_CLASSIFY_RULES:
        assert cat in dc.DOC_CATEGORIES, cat


def test_extract_priority_covers_every_category():
    """BUG 回归：旧内联优先级表只覆盖 6/10 类，其余被静默排到最后。"""
    for cat in dc.ALL_DOC_CATEGORIES:
        assert dc.extract_priority(cat) < dc.UNKNOWN_CATEGORY_PRIORITY, cat


def test_extract_priority_empty_treats_as_other():
    """存量行 doc_category 为空串时等价于「其他」，不能被挪到最末。"""
    assert dc.extract_priority("") == dc.extract_priority("其他")
    assert dc.extract_priority(None) == dc.extract_priority("其他")
    assert dc.extract_priority("   ") == dc.extract_priority("其他")


def test_unknown_category_goes_last():
    """前端手填/历史脏数据里的未知分类排最后，不影响既有分类的相对顺序。"""
    assert dc.extract_priority("随便写的分类") == dc.UNKNOWN_CATEGORY_PRIORITY
    assert dc.extract_priority("招标文件") < dc.extract_priority("其他")
    assert dc.extract_priority("其他") < dc.extract_priority("zzz")


def test_施工组织设计_not_misfiled_as_design():
    """BUG 回归：施工组织设计此前被「设计」宽关键词吞进「设计文件」。"""
    assert dc.auto_classify_document("施工组织设计.pdf") == "招标文件"
    assert dc.auto_classify_document("施工组织设计（投标版）.docx") == "招标文件"
    # 且提取优先级必须高于设计文件（施工组织设计是本平台的头号核心资料）
    assert (dc.extract_priority(dc.auto_classify_document("施工组织设计.pdf"))
            < dc.extract_priority("设计文件"))


@pytest.mark.parametrize("name,expected", [
    ("招标公告.pdf", "招标文件"),
    ("投标须知.pdf", "招标文件"),
    ("施工合同.pdf", "合同文件"),
    ("初步设计说明书.pdf", "设计文件"),
    ("施工图设计.pdf", "设计文件"),
    ("地勘勘察报告.pdf", "地勘报告"),
    ("工程量清单.xlsx", "报价清单"),
    ("综合单价分析.xlsx", "报价清单"),
    ("资质证书.pdf", "资质材料"),
    ("营业执照.pdf", "资质材料"),
    ("项目经理业绩.pdf", "人员资料"),
    ("财务审计报告.pdf", "财务资料"),
    ("类似工程业绩证明.pdf", "业绩证明"),
    ("README.txt", "其他"),
])
def test_autoclassify_existing_behaviors_unchanged(name, expected):
    """既有分类行为逐条钉住（重构只准修复、不准改变旧结论）。"""
    assert dc.auto_classify_document(name) == expected


async def test_category_options_endpoint_matches_single_source(ctx):
    """端点输出必须与单一事实源逐字一致（防再次漂移）。"""
    res = await gf.list_category_options()
    assert res["options"] == dc.category_options()
    assert [k["category"] for k in res["auto_keywords"]] == list(dc.DOC_CATEGORIES)
    for item in res["auto_keywords"]:
        assert isinstance(item["keywords"], list) and item["keywords"]


async def test_gf_alias_points_to_single_source():
    """global_facts 保留的 _DOC_CATEGORY_KEYWORDS 别名必须指向同一份数据。"""
    assert gf._DOC_CATEGORY_KEYWORDS == dc.AUTO_CLASSIFY_RULES
    assert gf._auto_classify_document("招标公告.pdf") == "招标文件"
    assert gf._auto_classify_document("zzz.txt") == "其他"


# ---------------------------------------------------------------------------
# 4. 提取合并顺序（跨模块数据传递）
# ---------------------------------------------------------------------------

def _doc(fid, cat, body="正文内容"):
    return {"id": fid, "file_name": f"{fid}.pdf",
            "parsed_markdown": f"文档{fid}：{body}", "doc_category": cat}


def test_combine_doc_texts_orders_by_priority():
    """合并顺序按分类优先级排列，且未知分类不再被误排。"""
    docs = [
        _doc("zz", "资质材料"), _doc("aa", "招标文件"),
        _doc("bb", "设计文件"), _doc("cc", ""), _doc("dd", "业绩证明"),
    ]
    out = ba._combine_doc_texts(docs)
    pos = {d["id"]: out.index(f"文档{d['id']}") for d in docs}
    # 招标文件 < 设计文件 < 其他(空) < 资质材料 < 业绩证明
    assert pos["aa"] < pos["bb"] < pos["cc"] < pos["zz"] < pos["dd"]


def test_combine_doc_texts_unknown_category_last():
    """反例：脏分类值不应插到正常分类之前。"""
    docs = [_doc("x", "随便写的分类"), _doc("y", "招标文件")]
    out = ba._combine_doc_texts(docs)
    assert out.index("文档y") < out.index("文档x")


def test_combine_doc_texts_skips_empty_body():
    """空正文文档不参与合并，且不报错。"""
    docs = [
        {"id": "a", "file_name": "a.pdf", "parsed_markdown": "",
         "doc_category": "招标文件"},
        {"id": "b", "file_name": "b.pdf", "parsed_markdown": "有效内容",
         "doc_category": "招标文件"},
    ]
    out = ba._combine_doc_texts(docs)
    assert "有效内容" in out and "a.pdf" not in out


def test_combine_doc_texts_empty_input():
    assert ba._combine_doc_texts([]) == ""


# ---------------------------------------------------------------------------
# 5. 分类选项单一事实源 + /documents 返回口径
# ---------------------------------------------------------------------------

def test_category_options_covers_all_categories_from_single_source():
    """护栏：下拉选项必须与唯一事实源一致，且覆盖全部分类 + 兜底「其他」。"""
    opts = dc.category_options()
    assert opts == list(dc.ALL_DOC_CATEGORIES)
    for cat in dc.DOC_CATEGORIES:
        assert cat in opts, cat
    # 规则表里出现的分类也必须在选项内（否则前端选不出后端能识别的类）
    for cat, _kws in dc.AUTO_CLASSIFY_RULES:
        assert cat in opts, cat


async def test_category_options_includes_fallback_other(ctx):
    """「其他」必须在选项中（历史脏数据/手填值的落点），且只出现一次。"""
    opts = dc.category_options()
    assert opts.count(dc.OTHER_CATEGORY) == 1
    assert opts[-1] == dc.OTHER_CATEGORY
    # 端点输出与单一事实源逐字一致
    data = await gf.list_category_options()
    assert data["options"] == opts
    assert [k["category"] for k in data["auto_keywords"]] == list(dc.DOC_CATEGORIES)


async def test_document_list_returns_text_len_parse_status_and_truncated(ctx):
    """BUG 回归：列表必须回传 text_len / parse_status，否则前端全显示 0 字符。"""
    db, pid, sid = ctx
    body = "这是招标文件的正文内容" * 10  # 260 字符
    doc_id = await _insert_legacy_doc(db, pid, body=body, status="success")

    data = await gf.list_documents(project_id=pid, db=db)

    row = next(r for r in data["documents"] if r["id"] == doc_id)
    assert row["parse_status"] == "success"
    assert row["text_len"] == len(body)
    assert isinstance(row["truncated"], bool) and row["truncated"] is False


async def test_document_list_requires_scope(ctx):
    """护栏：无 scheme_id / project_id 时不得跨项目返回全部文档。"""
    db, _pid, _sid = ctx
    await _insert_legacy_doc(db, _pid)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await gf.list_documents(db=db)
    assert exc.value.status_code == 400


async def test_document_list_does_not_overwrite_manual_category(ctx):
    """列表读取不得改写人工已设的 doc_category（分类是用户资产）。"""
    db, pid, _sid = ctx
    doc_id = await _insert_legacy_doc(db, pid, body="已有正文内容超过十个字符")
    await db.execute(
        "UPDATE project_documents SET doc_category='业绩证明' WHERE id=?", (doc_id,))
    await db.commit()

    data = await gf.list_documents(project_id=pid, db=db)
    row = next(r for r in data["documents"] if r["id"] == doc_id)
    assert row["doc_category"] == "业绩证明"

    cur = await db.execute(
        "SELECT doc_category FROM project_documents WHERE id=?", (doc_id,))
    assert (await cur.fetchone())["doc_category"] == "业绩证明"


# ---------------------------------------------------------------------------
# 6. 提取聚合口径：预算不被重复消耗 + 截断信号可观测
# ---------------------------------------------------------------------------

def _doc_with_len(name, cat, chars, *, text_len=None, parse_warnings=""):
    """构造聚合输入文档：parsed_markdown 精确为 chars 个字符。"""
    prefix = f"【{name}】"
    d = {
        "id": uuid.uuid4().hex, "file_name": name, "doc_category": cat,
        "parsed_markdown": prefix + ("文" * max(0, chars - len(prefix))),
    }
    if text_len is not None:
        d["text_len"] = text_len
    if parse_warnings:
        d["parse_warnings"] = parse_warnings
    return d


def _md_len(name, chars):
    """_doc_with_len 产出的正文实际长度（用于断言，避免硬编码字符计数）。"""
    return len(f"【{name}】") + max(0, chars - len(f"【{name}】"))


def test_combine_doc_texts_gives_each_file_full_budget():
    """BUG 修复：预算不再被入库截断重复消耗。

    旧实现入库时已按 _MAX_DOC_CHARS 截断 parsed_markdown，聚合时再按
    len(md) 切片 → 第二份 29000 字文档只剩 1000 字余量被吃掉。
    """
    docs = [
        _doc_with_len("招标.pdf", "招标文件", ba._MAX_DOC_CHARS),
        _doc_with_len("设计.pdf", "设计文件", ba._MAX_DOC_CHARS - 1000),
    ]
    out = ba._combine_doc_texts(docs)
    # 招标文件吃满预算 → 设计文件被整份跳过（而不是被切掉 28000 字）
    assert "【招标.pdf】" in out
    assert "【设计.pdf】" not in out
    assert "文" * 90 in out  # 招标文档的完整正文都在


def test_combine_doc_texts_truncation_reportable():
    """BUG 修复：入库截断文档必须显式标记 truncated=True。"""
    res = ba._combine_doc_texts(
        [_doc_with_len("截断.pdf", "招标文件", 5000, text_len=-1)],
        report_truncation=True)
    assert res["truncated"] is True
    assert res["details"][0]["name"] == "截断.pdf"
    assert res["total_chars"] > 0
    assert isinstance(res["text"], str) and "【截断.pdf】" in res["text"]


def test_combine_doc_texts_no_truncation_when_all_fresh():
    """反例：全部正文完整时不得误报 truncated。"""
    res = ba._combine_doc_texts([
        _doc_with_len("a.pdf", "招标文件", 1000),
        _doc_with_len("b.pdf", "设计文件", 800),
    ], report_truncation=True)
    assert res["truncated"] is False
    assert res["details"][0]["truncated"] is False
    assert res["total_chars"] == _md_len("a.pdf", 1000) + _md_len("b.pdf", 800)


def test_doc_is_truncated_uses_text_len_as_authority():
    """BUG 修复：截断判定以 text_len 列为准，而不是靠猜警告文本。"""
    assert ba._doc_is_truncated({"text_len": -1, "parse_warnings": ""}) is True
    assert ba._doc_is_truncated(
        {"text_len": -1, "parse_warnings": "完全无用的文本"}) is True
    assert ba._doc_is_truncated({"text_len": 12345, "parse_warnings": "x"}) is False
    # 旧库无该列 → 回退到 parse_warnings 文本
    assert ba._doc_is_truncated({"parse_warnings": "text truncated"}) is True
    assert ba._doc_is_truncated({"parse_warnings": "ocr downgraded"}) is False
    assert ba._doc_is_truncated({}) is False


def test_doc_text_len_falls_back_without_column():
    """旧库未补列时回退 len(parsed_markdown)，不会把文档误判为截断。"""
    assert ba._doc_text_len({"parsed_markdown": "一二三四"}) == 4
    assert ba._doc_text_len({"parsed_markdown": ""}) == 0
    assert ba._doc_text_len({}) == 0
    # 有列时以列为准（即使正文被二次裁剪）
    assert ba._doc_text_len({"parsed_markdown": "短", "text_len": 9999}) == 9999


async def test_list_parsed_documents_exposes_truncation_columns(ctx):
    """BUG 修复：聚合读路径必须 SELECT 出 parse_warnings 列。"""
    db, pid, sid = ctx
    for fid, name, cat, warn in [
        (uuid.uuid4().hex, "n.pdf", "招标文件", ""),
        (uuid.uuid4().hex, "t.pdf", "设计文件",
         '["text truncated: kept 30000 chars"]'),
    ]:
        await db.execute(
            "INSERT INTO project_documents(id, project_id, file_name, "
            "file_type, doc_category, parsed_markdown, parse_status, parse_warnings) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (fid, pid, name, "pdf", cat, "正文内容十个字符", "success", warn))
    await db.commit()

    docs = await ba._list_parsed_documents(db, pid)
    got = {d["file_name"]: d for d in docs}
    assert got["n.pdf"]["parse_warnings"] == ""
    assert got["t.pdf"]["parse_warnings"] == '["text truncated: kept 30000 chars"]'


def test_list_parsed_documents_fallback_when_column_missing():
    """反例：库表尚无 parse_warnings 列时，读取必须降级不报错。"""
    class _BoomDB:
        """模拟旧库：带 parse_warnings 的 SQL 报 no such column。"""

        async def execute(self, sql, params=None):
            if "parse_warnings" in sql:
                raise sqlite3.OperationalError("no such column: parse_warnings")

            class _Cur:
                async def fetchall(self):
                    return []

            return _Cur()

    import asyncio
    assert asyncio.run(ba._list_parsed_documents(_BoomDB(), "p1")) == []


def test_combine_doc_texts_skips_empty_budget():
    """反例：预算耗尽后后续文档整份跳过，不产生空段落。"""
    docs = [
        _doc_with_len("满.pdf", "招标文件", ba._MAX_DOC_CHARS),
        _doc_with_len("剩.pdf", "设计文件", 500),
    ]
    out = ba._combine_doc_texts(docs)
    assert out.count("---") == 0  # 只有 1 份文档 → 无分隔线
    assert "【剩.pdf】" not in out


def test_combine_doc_texts_reports_details_in_priority_order():
    """details 顺序与拼接顺序一致（招标 → 设计 → 其他 → 业绩）。"""
    res = ba._combine_doc_texts([
        _doc_with_len("业绩.pdf", "业绩证明", 100),
        _doc_with_len("招标.pdf", "招标文件", 100),
        _doc_with_len("设计.pdf", "设计文件", 100),
        _doc_with_len("其他.pdf", "其他", 100),
    ], report_truncation=True)
    assert [d["name"] for d in res["details"]] == [
        "招标.pdf", "设计.pdf", "其他.pdf", "业绩.pdf"]


def test_combine_doc_texts_back_compat_without_text_len():
    """旧数据（无 text_len 键）不得被误判为截断。"""
    docs = [{"id": "x", "file_name": "old.pdf", "doc_category": "招标文件",
             "parsed_markdown": "历史正文内容十个字符"}]
    res = ba._combine_doc_texts(docs, report_truncation=True)
    assert res["truncated"] is False
    assert res["details"][0]["chars"] == len("历史正文内容十个字符")



