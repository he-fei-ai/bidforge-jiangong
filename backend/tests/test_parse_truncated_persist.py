"""F2 回归（2026-09-26）：解析器级截断（PDF 截页 / 表格截行）持久化到
``project_documents.parse_truncated``，列表接口 ``load_documents`` 直接读取，
刷新后不再漏报；旧库（该列为空）仍按字数反推兜底。

覆盖：
- 解析器级截断（字数很小）应被列表直接读出 truncated=True；
- 无截断小文档 truncated=False；
- 字数达上限（旧口径）仍判定 truncated=True（回退逻辑不退化）。
- 迁移补列：parse_truncated 列必须存在（防 schema 漂移）。
"""
import uuid

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.routers import global_facts


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid
    await db.close()


async def _insert_doc(db, pid, doc_id, text_len, parse_truncated):
    # 列表接口的 text_len 来自 length(parsed_markdown)，这里直接写入对应长度的
    # 正文以模拟落库字数；parse_truncated 为本次新增的持久化截断标记。
    stored = "x" * text_len
    await db.execute(
        "INSERT INTO project_documents(id, project_id, file_name, parsed_markdown, "
        "parse_truncated, parse_status) VALUES(?,?,?,?,?,?)",
        (doc_id, pid, doc_id, stored, parse_truncated, "success"))
    await db.commit()


async def test_parse_truncated_persisted_and_listed(ctx):
    db, pid = ctx
    # ① 解析器级截断（如 PDF 截页）：字数很小，旧逻辑（仅按字数反推）会漏报
    await _insert_doc(db, pid, "a", text_len=100, parse_truncated=1)
    # ② 正常小文档：无截断
    await _insert_doc(db, pid, "b", text_len=100, parse_truncated=0)
    # ③ 旧口径兜底：字数达上限（无 parse_truncated 列时为 NULL→0）仍应判定截断
    await _insert_doc(db, pid, "c", text_len=global_facts.MAX_PARSED_CHARS + 10,
                      parse_truncated=0)

    res = await global_facts.list_documents(project_id=pid, scheme_id="", db=db)
    docs = {d["id"]: d for d in res["documents"]}
    assert docs["a"]["truncated"] is True, "解析器级截断应被列表直接读出"
    assert docs["b"]["truncated"] is False, "正常文档不应被标记截断"
    assert docs["c"]["truncated"] is True, "字数达上限仍应判定截断（回退口径）"


async def test_parse_truncated_column_exists(ctx):
    db, _pid = ctx
    cur = await db.execute("PRAGMA table_info(project_documents)")
    cols = {r["name"] for r in await cur.fetchall()}
    assert "parse_truncated" in cols, "迁移必须补列 parse_truncated"
