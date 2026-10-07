"""解析提取模块 · 截断判定跨读路径口径统一回归（2026-10-05）

修复项：``global_facts._query_docs_with_diag``（供 ``load_parsed_docs`` /
``load_parsed_texts`` 消费，即目录 / 正文 / 事实生成链路）此前用**互斥**
``if/elif`` 判定截断 —— 只在「``project_documents`` 没有 ``parse_truncated``
列」时才回退看 ``parse_warnings`` 文本。于是两类存量行被漏报为「未截断」：

  ① 该列上线（2026-09-26）之前的存量行：``parse_truncated`` 恒为 0，
     但 ``parse_warnings`` 早已写入「已截断」告警；
  ② 旧版按 80000 字截断、且告警已丢失的行：只能靠落库正文字数等于
     历史 / 当前上限来识别。

后果：``truncated_out`` 恒为空、WARNING 不落日志，用户与下游都无从得知
「生成依据不完整」；且本模块的列表（``list_documents``）/ 预览
（``preview_document``）也各自缺「告警文本兜底」，与 :mod:`app.routers.bid_analysis`
的 ``_doc_is_truncated`` 口径不一致 —— 同一份文档在不同页面给出不同答案。

本文件钉住三件事：
  A. ``gf.doc_is_truncated`` 与 ``ba._doc_is_truncated`` 口径一致（parity 矩阵）；
  B. ``load_parsed_docs`` 能识别上述两类存量行（含 ``truncated_out`` 回传）；
  C. 列表 / 预览 / 正文读取三条路径对同一份文档给出同一答案。
"""
from __future__ import annotations

import uuid

import app.routers.bid_analysis as ba
import app.routers.global_facts as gf
import pytest
from app.services.file_parser import dump_parse_warnings

# ---------------------------------------------------------------------------
# A. 判定口径 parity（gf 与 ba 必须完全一致）
# ---------------------------------------------------------------------------

_BIG = "x" * gf.MAX_PARSED_CHARS
_LEGACY = "x" * 80_000
_JUST_BELOW = "x" * (gf.MAX_PARSED_CHARS - 1)

_PARITY_CASES = [
    ("no_signal", {}),
    ("flag_1", {"parse_truncated": 1, "parsed_markdown": "短正文"}),
    ("flag_0_only", {"parse_truncated": 0, "parsed_markdown": "短正文"}),
    ("flag_bool_true", {"parse_truncated": True, "parsed_markdown": "短正文"}),
    ("warn_zh", {"parse_truncated": 0, "parsed_markdown": "短正文",
                 "parse_warnings": "原文 400000 字，已截断至 400000 字上限"}),
    ("warn_pdf", {"parse_truncated": 0, "parsed_markdown": "短正文",
                  "parse_warnings": "第 50 页之后已截断，建议拆分文件"}),
    ("warn_en", {"parse_truncated": 0, "parsed_markdown": "短正文",
                 "parse_warnings": "content truncated at limit"}),
    ("warn_list", {"parse_truncated": 0, "parsed_markdown": "短正文",
                   "parse_warnings": ["第 3 页之后已截断"]}),
    ("warn_irrelevant", {"parse_truncated": 0, "parsed_markdown": "短正文",
                         "parse_warnings": "ocr downgraded"}),
    ("sentinel_neg1", {"text_len": -1, "parsed_markdown": "短正文"}),
    ("char_at_max", {"parse_truncated": 0, "parsed_markdown": _BIG}),
    ("char_at_legacy", {"parse_truncated": 0, "parsed_markdown": _LEGACY}),
    ("char_below_max", {"parse_truncated": 0, "parsed_markdown": _JUST_BELOW}),
]


@pytest.mark.parametrize(("label", "doc"), _PARITY_CASES,
                         ids=[c[0] for c in _PARITY_CASES])
def test_predicate_parity_between_modules(label, doc):
    """gf 的统一判定必须与 ba 的既有判定逐例一致，杜绝口径再次漂移。"""
    assert gf.doc_is_truncated(doc) == ba._doc_is_truncated(doc), (
        f"截断判定口径漂移（{label}）：gf={gf.doc_is_truncated(doc)} "
        f"ba={ba._doc_is_truncated(doc)}")


# ---------------------------------------------------------------------------
# B / C. DB 级：存量行识别 + 三条读路径一致
# ---------------------------------------------------------------------------

_COLS = ("id, project_id, file_name, file_type, parsed_markdown, "
         "doc_category, parse_warnings, parse_truncated, parse_status, "
         "created_at")


async def _insert_doc(db, pid, name, body, *, trunc=0, warnings=None,
                      created_at="2026-01-01 00:00:00"):
    doc_id = uuid.uuid4().hex
    await db.execute(
        f"INSERT INTO project_documents({_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (doc_id, pid, name, name.rsplit(".", 1)[-1], body, "其他",
         dump_parse_warnings(warnings or []), trunc, "success", created_at))
    await db.commit()
    return doc_id


async def _seed_project(db):
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    return pid


@pytest.mark.asyncio
async def test_load_parsed_docs_flags_legacy_warning_row(db_conn):
    """① 存量行：parse_truncated=0，但 parse_warnings 含「截断」→ 必须被识别。

    旧实现（互斥 elif）在「有 parse_truncated 列」时**根本不看告警文本**，
    本用例正是当时恒假通过的场景。
    """
    db = db_conn
    pid = await _seed_project(db)
    await _insert_doc(db, pid, "存量告警.pdf", "正文A", trunc=0,
                      warnings=["第 50 页之后已截断，建议拆分文件"],
                      created_at="2026-01-01 00:00:00")
    await _insert_doc(db, pid, "正常.txt", "正文B", trunc=0,
                      created_at="2026-01-02 00:00:00")

    out: list = []
    pairs = await gf.load_parsed_docs(db, pid, None, out)
    assert {n for n, _t in pairs} == {"存量告警.pdf", "正常.txt"}
    assert out == ["存量告警.pdf"], \
        "parse_truncated=0 但告警含「截断」的存量行必须被识别"


@pytest.mark.asyncio
async def test_load_parsed_docs_flags_legacy_char_limit_row(db_conn):
    """② 旧版 80000 字截断、告警已丢失 → 靠落库长度等于历史上限识别。"""
    db = db_conn
    pid = await _seed_project(db)
    await _insert_doc(db, pid, "旧版截断.md", "x" * 80_000, trunc=0,
                      created_at="2026-01-01 00:00:00")
    await _insert_doc(db, pid, "完整.md", "y" * 100, trunc=0,
                      created_at="2026-01-02 00:00:00")

    out: list = []
    await gf.load_parsed_docs(db, pid, None, out)
    assert out == ["旧版截断.md"]


@pytest.mark.asyncio
async def test_load_parsed_docs_no_false_positive(db_conn):
    """正常短正文不得被误报截断（防止口径放宽引入假阳性）。"""
    db = db_conn
    pid = await _seed_project(db)
    await _insert_doc(db, pid, "正常.txt", "正文", trunc=0,
                      warnings=["ocr 已降级为文本提取"])
    out: list = []
    await gf.load_parsed_docs(db, pid, None, out)
    assert out == []


@pytest.mark.asyncio
async def test_three_read_paths_agree_on_truncation(db_conn):
    """列表 / 预览 / 正文读取三条路径对同一份文档给出同一答案（跨模块一致）。"""
    db = db_conn
    pid = await _seed_project(db)
    warn_id = await _insert_doc(db, pid, "告警行.pdf", "正文A", trunc=0,
                                warnings=["第 50 页之后已截断"])
    ok_id = await _insert_doc(db, pid, "完整.txt", "正文B", trunc=0)

    listed = {d["file_name"]: d["truncated"]
              for d in (await gf.list_documents(
                  project_id=pid, scheme_id="", db=db))["documents"]}
    out: list = []
    await gf.load_parsed_docs(db, pid, None, out)
    prev_warn = await gf.preview_document(warn_id, max_chars=5000, db=db)
    prev_ok = await gf.preview_document(ok_id, max_chars=5000, db=db)

    assert listed["告警行.pdf"] is True, "列表页必须显示「可能被截断」"
    assert listed["完整.txt"] is False
    assert out == ["告警行.pdf"], "正文读取必须把该行计入 truncated_out"
    assert prev_warn["truncated"] is True, "预览必须透传解析级截断"
    assert prev_ok["truncated"] is False