"""解析提取模块 · 截断信号跨模块贯通回归（2026-09-26）

本文件钉住四处「信号已产生但传不到用户」的断链，全部来自真实代码缺陷：

1. **`_doc_is_truncated` 判定恒为 False（跨模块数据传递断裂，高）**
   旧实现只认两条**结构性不可达**的路径 —— 「不存在的 ``text_len`` 列 == -1」
   （``project_documents`` 表从未有该列，本模块 SELECT 也从未取过它）与「``parse_warnings`` 里出现英文
   ``truncated``」（而 ``file_parser`` 的告警全是中文，如「原文 X 字，已截断至
   N 字上限」）。后果：18 项结构化提取在残缺资料上照常提取，前端无从得知
   「提取依据不完整」。

2. **``report_truncation=True`` 分支从未被调用（调用链路断裂）**
   ``_combine_doc_texts`` 的报告分支自 2026-09-25 加入起 4 处调用全走字符串
   默认分支，截断信号停在后端。

3. **SELECT 缺列降级「一刀切」（信息损失）**
   ``_list_parsed_documents`` 旧实现在整句 SELECT 报 OperationalError 时直接
   回退到基础列，新增 ``parse_truncated`` 后会连带丢掉已有的 ``parse_warnings``。

4. **预览接口缺解析级截断信号（用户可感知性）**
   ``/documents/{id}/preview`` 只有 ``preview_truncated``（"预览只截取前 N 字"），
   解析器级截断（PDF 超页）在预览弹窗里完全不可见。

测试直接调用路由/服务函数（与 test_bid_analysis.py 风格一致），
避免 TestClient 跨事件循环持有 aiosqlite 连接导致的偶发失败。
"""
from __future__ import annotations

import json
import uuid

import pytest

import app.routers.bid_analysis as ba
import app.routers.global_facts as gf
from app.services.file_parser import MAX_PARSE_WARNINGS, dump_parse_warnings


# ---------------------------------------------------------------------------
# 不变量 1：_doc_is_truncated 四层判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc", "expected", "reason"),
    [
        # ① 权威口径：解析阶段持久化的标记
        ({"parse_truncated": 1, "parsed_markdown": "很短的正文"},
         True, "parse_truncated=1 必须判定为截断"),
        ({"parse_truncated": 0, "parsed_markdown": "很短的正文"},
         False, "parse_truncated=0 且无其它信号 → 不截断"),
        # ② 中文告警兜底（file_parser 的真实输出形态）
        ({"parse_truncated": 0, "parsed_markdown": "x" * 20,
          "parse_warnings": "原文 400000 字，已截断至 120000 字上限"},
         True, "中文告警含「截断」必须判定为截断"),
        ({"parse_truncated": 0, "parsed_markdown": "x" * 20,
          "parse_warnings": "PDF 第 50 页之后已截断，建议拆分文件"},
         True, "PDF 截页类中文告警必须判定为截断"),
        # ② 兼容历史/外部写入的英文告警
        ({"parse_truncated": 0, "parsed_markdown": "x" * 20,
          "parse_warnings": "content truncated at limit"},
         True, "英文 truncated 仍须兼容"),
        # parse_warnings 可能被外部写成分数组（历史写入形态）
        ({"parse_truncated": 0, "parsed_markdown": "x" * 20,
          "parse_warnings": ["第 3 页之后已截断"]},
         True, "数组形态的告警也必须判定"),
        ({"parse_truncated": 0, "parsed_markdown": "x" * 20},
         False, "小正文无信号 → 不截断"),
        ({"parse_truncated": 0, "parsed_markdown": ""},
         False, "空正文 → 不截断"),
        # 旧版入库截断哨兵值 -1：保留其「已截断」语义（既有契约，见
        # test_import_module_fixes_20260925.py::test_doc_is_truncated_*）。
        # 生产路径不会产出 -1（本模块 SELECT 不取 text_len 列，_doc_text_len
        # 缺列时回退 len(parsed_markdown) ≥ 0），保留仅为了不破坏既有契约；
        # 方向是保守的 —— 宁可多提示，也不能让残缺资料被当成完整资料。
        ({"text_len": -1, "parsed_markdown": "x" * 20},
         True, "旧版入库截断哨兵值 -1 仍须判定为截断"),
    ],
)
def test_doc_is_truncated_layers(doc, expected, reason):
    assert ba._doc_is_truncated(doc) is expected, reason


def test_doc_is_truncated_char_limit_fallback():
    """② 字数达落库上限 → 即便 parse_truncated 仍为 0/NULL 也判定截断。

    覆盖该列上线（2026-09-26）之前就已因超长被截断、标记尚未回填的存量行。
    """
    big = {"parse_truncated": 0, "parsed_markdown": "x" * gf.MAX_PARSED_CHARS}
    assert ba._doc_is_truncated(big) is True, "达当前上限必须判定为截断"
    # 历史上限（80000 字）也要认，与 global_facts._LEGACY_PARSE_LIMITS 对齐
    legacy = {"parse_truncated": 0, "parsed_markdown": "x" * 80_000}
    assert ba._doc_is_truncated(legacy) is True, "历史 80000 字上限必须判定为截断"
    # 上限减一字 → 不截断（避免把「接近上限的正常文档」误报）
    just_below = {"parse_truncated": 0,
                  "parsed_markdown": "x" * (gf.MAX_PARSED_CHARS - 1)}
    assert ba._doc_is_truncated(just_below) is False


def test_doc_is_truncated_parse_truncated_wins_over_warnings():
    """标记为 0 但告警文本含「截断」→ 仍判定为截断（四层是 OR 关系）。"""
    doc = {"parse_truncated": 0, "parsed_markdown": "x" * 10,
           "parse_warnings": "解析器判定内容不完整（PDF 页数截断）"}
    assert ba._doc_is_truncated(doc) is True


# ---------------------------------------------------------------------------
# 不变量 2：_list_parsed_documents 按真实列集合降级（缺哪列只丢哪列）
# ---------------------------------------------------------------------------


_DOC_COLS = ("id, project_id, file_name, file_type, parsed_markdown, "
             "doc_category, parse_warnings, parse_truncated, parse_status, "
             "parse_time")


async def _seed_docs(db, pid: str) -> list[str]:
    """写入三份已解析文档，分别覆盖三种截断信号来源（标记/告警/无）。"""
    ids = [uuid.uuid4().hex for _ in range(3)]
    rows = [
        (ids[0], "招标文件.pdf", "pdf", "正文A", "招标文件",
         "第 50 页之后已截断", 1, 1.0),
        (ids[1], "地勘报告.docx", "docx", "正文B", "地勘报告", "[]", 0, 2.0),
        (ids[2], "合同.doc", "doc", "正文C", "合同文件", "", 0, 3.0),
    ]
    for doc_id, fname, ftype, body, cat, warnings, trunc, secs in rows:
        await db.execute(
            f"INSERT INTO project_documents({_DOC_COLS}) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (doc_id, pid, fname, ftype, body * 10, cat, warnings, trunc,
             "success", secs))
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_list_parsed_documents_returns_truncation_columns(db_conn):
    """迁移后的正常库：两列诊断信息必须都在，且截断判定随之正确。"""
    db = db_conn
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    ids = await _seed_docs(db, pid)

    docs = await ba._list_parsed_documents(db, pid)
    assert len(docs) == 3, "三份文档全部返回"
    by_id = {d["id"]: d for d in docs}
    for i in ids:
        assert "parse_warnings" in by_id[i], "parse_warnings 列必须保留"
        assert "parse_truncated" in by_id[i], "parse_truncated 列必须新增返回"

    assert ba._doc_is_truncated(by_id[ids[0]]) is True, "标记=1 应贯通判定"
    assert ba._doc_is_truncated(by_id[ids[1]]) is False
    assert ba._doc_is_truncated(by_id[ids[2]]) is False


@pytest.mark.asyncio
async def test_list_parsed_documents_missing_column_degrades_gracefully(db_conn):
    """缺列时必须降级而不是报错，且不得连带丢掉另一列诊断信息。

    回归点：旧实现在整句 SELECT 报 OperationalError 时一刀切回退到基础列，
    新增 parse_truncated 后会连带把已存在的 parse_warnings 也丢掉 ——
    「写而不读」重新出现，解析告警再次静默丢失。
    """
    db = db_conn
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    ids = await _seed_docs(db, pid)

    # 模拟「只补了 parse_warnings、没补 parse_truncated」的半迁移库
    await db.execute("ALTER TABLE project_documents DROP COLUMN parse_truncated")
    await db.commit()

    docs = await ba._list_parsed_documents(db, pid)  # 不得抛 OperationalError
    assert len(docs) == 3
    d0 = {d["id"]: d for d in docs}[ids[0]]
    assert "parse_warnings" in d0, "缺 parse_truncated 时 parse_warnings 必须保留"
    assert "parse_truncated" not in d0
    # 降级后仍靠告警文本兜底判定出截断，不因列缺失而漏报
    assert ba._doc_is_truncated(d0) is True


@pytest.mark.asyncio
async def test_list_parsed_documents_table_without_both_columns(db_conn):
    """两列都不存在（迁移前旧库）→ 只返回基础列，读路径始终可用。"""
    db = db_conn
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    ids = await _seed_docs(db, pid)

    await db.execute("ALTER TABLE project_documents DROP COLUMN parse_truncated")
    await db.execute("ALTER TABLE project_documents DROP COLUMN parse_warnings")
    await db.commit()

    docs = await ba._list_parsed_documents(db, pid)
    assert len(docs) == 3
    d0 = {d["id"]: d for d in docs}[ids[0]]
    assert set(d0) == {"id", "file_name", "file_type", "parsed_markdown",
                       "doc_category", "file_size", "parse_time"}
    assert ba._doc_is_truncated(d0) is False, "无诊断信息时不得误报截断"


# ---------------------------------------------------------------------------
# 不变量 3：_combine_doc_texts_report（把从未被调用的死分支接通）
# ---------------------------------------------------------------------------


def _mk_doc(name: str, chars: int, truncated: bool = False,
            category: str = "招标文件") -> dict:
    """构造一份已解析文档行（与 _list_parsed_documents 返回形状一致）。"""
    return {
        "id": uuid.uuid4().hex,
        "file_name": name,
        "file_type": "pdf",
        "doc_category": category,
        "parsed_markdown": name + " " + ("字" * chars),
        "parse_warnings": "已截断" if truncated else "[]",
        "parse_truncated": 1 if truncated else 0,
        "text_len": 0,
    }


def test_report_flags_truncated_source_document():
    """任一参与提取的文档被截断 → source_truncated=True 且给出文件名清单。"""
    docs = [_mk_doc("招标文件.pdf", 12_000, truncated=True),
            _mk_doc("合同.doc", 3_000, category="合同文件")]
    text, report = ba._combine_doc_texts_report(docs)
    assert text.strip(), "合并文本不得为空"
    assert report["source_truncated"] is True
    # details 沿用 _combine_doc_texts 既有的 "name" 键（与 _MAX_DOC_CHARS 预算
    # 切片共用同一份结构），报告层不再另造键名
    assert [d["name"] for d in report["truncated_docs"]] == ["招标文件.pdf"]
    assert report["used_doc_count"] == 2
    assert report["input_doc_count"] == 2
    assert report["dropped_doc_count"] == 0
    # total_chars = 各文档正文片段字数之和（不含拼接时插入的文档标题行），
    # 两份文档均在提取预算内 → 等于原文正文总长
    assert report["total_chars"] == sum(len(d["parsed_markdown"]) for d in docs)
    # 拼接时会插入「# 文档：X（分类：Y）」标题行，故最终文本更长
    assert len(text) >= report["total_chars"]
    assert len(text) >= report["total_chars"]


def test_report_clean_when_all_docs_complete():
    """全部文档完整 → source_truncated=False、清单为空（不得误报）。"""
    docs = [_mk_doc("招标文件.pdf", 8_000),
            _mk_doc("地勘报告.docx", 4_000, category="地勘报告")]
    _text, report = ba._combine_doc_texts_report(docs)
    assert report["source_truncated"] is False
    assert report["truncated_docs"] == []
    assert report["dropped_doc_count"] == 0


def test_report_flags_docs_dropped_by_budget():
    """预算耗尽被整份跳过的文档 → dropped_doc_count>0 且 source_truncated=True。

    这类文档一个字都没进入提取，但内容本身并不缺 —— 用户同样需要知道
    「第二份资料完全没被用上」。

    ⚠️ 文档规模必须相对 ``_MAX_DOC_CHARS`` 构造，**不可写死 30000**：
    提取预算已由 A-1（第十七轮）从 30000 提升到 400000（与 ``file_parser``
    的 ``MAX_PARSED_CHARS`` / 入库上限对齐），写死 30000 的旧数据在当前
    默认配置下**总长不超预算** → 本用例恒假失败，且掩盖了「预算对齐」这一
    修复。故按实际预算推导规模，并在断言中把预算显式打印出来便于排障。

    注意「整份跳过」要求**预算被第一份耗尽**（``headroom == 0`` 才 break）；
    若第一份只占预算的一部分，第二份会被**切片**纳入 → ``used_doc_count==2``、
    ``dropped_doc_count==0``（那是「软截断」而非「整份丢弃」，另有用例覆盖）。
    故第一份取预算的 1.2 倍（单份就超预算），第二份必然被整份丢弃。
    """
    budget = ba._MAX_DOC_CHARS
    docs = [_mk_doc("招标文件.pdf", int(budget * 1.2)),
            _mk_doc("地勘报告.docx", int(budget * 0.5),
                    category="地勘报告")]
    _text, report = ba._combine_doc_texts_report(docs)
    assert report["dropped_doc_count"] == 1, \
        f"第二份应被整份跳过（预算={budget}）：{report}"
    assert report["source_truncated"] is True
    assert report["used_doc_count"] == 1


def test_report_text_identical_to_legacy_string_branch():
    """接通报告分支后，合并文本语义必须与旧字符串返回逐字一致（无回归）。"""
    docs = [_mk_doc("招标文件.pdf", 5_000),
            _mk_doc("合同.doc", 2_000, category="合同文件", truncated=True),
            _mk_doc("空文档.txt", 0)]
    legacy = ba._combine_doc_texts(docs)
    new_text, _report = ba._combine_doc_texts_report(docs)
    assert new_text == legacy, "报告分支不得改变合并文本内容"


def test_report_empty_docs_contract():
    """空文档列表：报告形状仍完整（供端点契约对称复用）。"""
    text, report = ba._combine_doc_texts_report([])
    assert text == ""
    assert report == {"source_truncated": False, "truncated_docs": [],
                      "used_doc_count": 0, "input_doc_count": 0,
                      "dropped_doc_count": 0, "total_chars": 0}


def test_report_keys_are_stable():
    """报告字段集合固定，供前端与四个端点统一判读（防止各端点自行漂移）。"""
    assert set(ba._combine_doc_texts_report([])[1]) == {
        "source_truncated", "truncated_docs", "used_doc_count",
        "input_doc_count", "dropped_doc_count", "total_chars"}


# ---------------------------------------------------------------------------
# 不变量 4：端点契约 —— 报告字段必须出现在每个提取入口的响应里
# ---------------------------------------------------------------------------

_REPORT_KEYS = {"source_truncated", "truncated_docs", "used_doc_count",
                "input_doc_count", "dropped_doc_count", "total_chars"}


async def _seed_scheme(db) -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (sid, pid, "s"))
    await db.commit()
    return pid, sid


@pytest.mark.asyncio
async def test_check_sections_response_carries_source_report(db_conn, monkeypatch):
    """/check-sections 三个返回分支都必须带提取依据完整性字段（契约对称）。"""
    db = db_conn
    _pid, sid = await _seed_scheme(db)

    async def _no_docs(_db, _pid):
        return []
    monkeypatch.setattr(ba, "_list_parsed_documents", _no_docs)
    r = await ba.check_bid_sections(scheme_id=sid, project_id="", db=db)
    assert r["source"] == "no_documents"
    assert _REPORT_KEYS <= set(r), f"无文档分支缺报告字段：{sorted(r)}"
    assert r["source_truncated"] is False

    # 文档存在但合并后为空
    async def _empty_docs(_db, _pid):
        return [{"id": "d1", "file_name": "空白.txt", "doc_category": "其他",
                 "parsed_markdown": "   ", "parse_warnings": "[]",
                 "parse_truncated": 0}]
    monkeypatch.setattr(ba, "_list_parsed_documents", _empty_docs)
    r = await ba.check_bid_sections(scheme_id=sid, project_id="", db=db)
    assert r["source"] == "empty_text"
    assert _REPORT_KEYS <= set(r)

    # 正常检测路径
    async def _ok_docs(_db, _pid):
        return [_mk_doc("招标文件.pdf", 3_000)]
    monkeypatch.setattr(ba, "_list_parsed_documents", _ok_docs)
    r = await ba.check_bid_sections(scheme_id=sid, project_id="", db=db)
    assert r["ok"] is True
    assert _REPORT_KEYS <= set(r), f"正常分支缺报告字段：{sorted(r)}"
    assert r["used_doc_count"] == 1


@pytest.mark.asyncio
async def test_extract_sections_response_carries_source_report(db_conn, monkeypatch):
    """/extract-sections 的成功响应必须带提取依据完整性。"""
    db = db_conn
    _pid, sid = await _seed_scheme(db)

    src_doc = _mk_doc("招标文件.pdf", 3_000, truncated=True)

    async def _fake_list(_db, _pid):
        return [src_doc]
    monkeypatch.setattr(ba, "_list_parsed_documents", _fake_list)

    sections = [{"id": "sec-1", "title": "第一标段", "description": "",
                 "evidence": [], "start_line": 1, "end_line": 1}]

    async def _fake_extract(*, markdown):
        return {"sections": sections, "segment_count": 1, "estimated_calls": 1}
    import app.services.bid_section_extraction as bse
    monkeypatch.setattr(bse, "extract_bid_sections", _fake_extract)

    r = await ba.extract_bid_sections_api(scheme_id=sid, project_id="", db=db)
    assert r["ok"] is True
    assert r["source_truncated"] is True
    assert [d["name"] for d in r["truncated_docs"]] == ["招标文件.pdf"]
    assert r["truncated_docs"][0]["chars"] == len(src_doc["parsed_markdown"])


# ---------------------------------------------------------------------------
# 不变量 5：预览接口契约对称 —— truncated（解析级）与 preview_truncated（预览级）
# ---------------------------------------------------------------------------


async def _seed_doc(db, pid: str, file_name: str, body: str, *,
                    parse_truncated: int = 0,
                    warnings: list[str] | None = None) -> str:
    """写入一份已解析文档行；告警按真实落库形态（JSON 数组串）存储。"""
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents(id, project_id, file_name, file_type, "
        "parsed_markdown, doc_category, parse_warnings, parse_truncated, "
        "parse_status) VALUES(?,?,?,?,?,?,?,?,?)",
        (doc_id, pid, file_name, file_name.rsplit(".", 1)[-1], body, "其他",
         dump_parse_warnings(warnings or []), parse_truncated, "success"))
    await db.commit()
    return doc_id


@pytest.mark.asyncio
async def test_preview_returns_parse_level_truncated(db_conn):
    """解析级截断必须透传到预览接口，且与预览截取互不覆盖。"""
    db = db_conn
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()

    # ① 解析器级截断（PDF 截页）：正文很短 → preview_truncated 必为 False
    short_id = await _seed_doc(
        db, pid, "超长图纸.pdf", "x" * 500, parse_truncated=1,
        warnings=["第 50 页之后已截断"])
    # ② 完全正常的文档
    ok_id = await _seed_doc(db, pid, "完整说明.txt", "y" * 500)
    # ③ 正文长 → 只被预览截取
    big_id = await _seed_doc(db, pid, "长文本.md", "z" * 20_000)
    # ④ 未解析
    unparsed_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents(id, project_id, file_name, "
        "parsed_markdown, parse_status) VALUES(?,?,?,?,?)",
        (unparsed_id, pid, "未解析.txt", "", ""))
    await db.commit()

    r1 = await gf.preview_document(short_id, max_chars=5000, db=db)
    assert r1["truncated"] is True, "解析级截断必须透传到预览接口"
    assert r1["preview_truncated"] is False, "正文很短 → 预览未被截取"
    assert r1["text_len"] == 500
    assert r1["parse_warnings"] == ["第 50 页之后已截断"]

    r2 = await gf.preview_document(ok_id, max_chars=5000, db=db)
    assert r2["truncated"] is False
    assert r2["preview_truncated"] is False

    r3 = await gf.preview_document(big_id, max_chars=5000, db=db)
    assert r3["preview_truncated"] is True
    assert r3["truncated"] is False, "预览截取不得冒用解析级截断语义"

    r4 = await gf.preview_document(unparsed_id, max_chars=5000, db=db)
    assert r4["truncated"] is False
    assert r4["preview_truncated"] is False
    assert r4["is_parsed"] is False

    # 两条路径字段集合一致（既有契约对称护栏 + 新增字段）
    assert set(r1) == set(r4), (
        f"预览接口两条返回路径字段集合必须一致：{sorted(set(r1) ^ set(r4))}")


@pytest.mark.asyncio
async def test_preview_legacy_char_limit_doc_is_truncated(db_conn):
    """字数达上限的历史文档（parse_truncated 未回填）→ 预览仍判定为截断。"""
    db = db_conn
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    doc_id = await _seed_doc(
        db, pid, "历史大文件.pdf", "x" * gf.MAX_PARSED_CHARS, parse_truncated=0)
    r = await gf.preview_document(doc_id, max_chars=5000, db=db)
    assert r["truncated"] is True, "字数上限回退口径必须生效"
    assert r["preview_truncated"] is True, "预览本身也被截取"


# ---------------------------------------------------------------------------
# 不变量 6：告警持久化上限护栏（防 dump_parse_warnings 静默吞掉关键告警）
# ---------------------------------------------------------------------------


def test_parse_warnings_cap_keeps_json_valid():
    """告警条数超上限时序列化结果仍是合法 JSON。

    历史 BUG：直接对序列化后的 JSON 字符串做字节切片，告警多时会截成非法串，
    读取端 json.loads 失败后返回 []，导致**全部告警静默丢失**（截断提示
    随之消失，用户以为资料完整）。
    """
    many = [f"告警 {i}" for i in range(MAX_PARSE_WARNINGS + 50)]
    back = json.loads(dump_parse_warnings(many))
    assert isinstance(back, list)
    assert len(back) == MAX_PARSE_WARNINGS, "按条数截断，不得越界"


def test_parse_warnings_long_single_item_truncated_marked():
    """超长单条告警会被截短并带省略号，但 JSON 仍然合法。"""
    back = json.loads(dump_parse_warnings(["x" * 10_000]))
    assert len(back) == 1
    assert len(back[0]) < 10_000
    assert back[0].endswith("…"), "必须显式标注被截短"


def test_parse_warnings_empty_and_none_inputs():
    """空/None 输入不得产出非法 JSON，读取端必须拿到空数组。"""
    assert json.loads(dump_parse_warnings([])) == []
    assert json.loads(dump_parse_warnings(None)) == []
    assert gf._decode_parse_warnings("") == []
    assert gf._decode_parse_warnings("not-json") == []
    assert gf._decode_parse_warnings('{"not": "a list"}') == []




