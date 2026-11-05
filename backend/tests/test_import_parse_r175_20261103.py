"""解析提取模块 R175 修复配套测试（2026-11-03）。

覆盖本轮四项修复（与 R53/R72/R73/R79/R171 既有护栏不重复）：

  BUG-2（P2）：UTF-16（含 BOM / 无 BOM）识别，避免 GBK 误判成功产生乱码入库；
  BUG-3（P3）：孤立 CR（旧式 Mac 行尾）必须告警留痕，不得静默；
  G7（P3）：落库截断改为「截到最后一个完整换行」，超长单行回退硬截；
  BUG-1（P1）：并发同名上传单行化（upsert）+ 旧库去重迁移 + 唯一索引。

测试风格与既有模块一致：直接调用路由函数 / 纯函数，不引入 TestClient。
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
import uuid
from pathlib import Path

import aiosqlite
import app.db as _appdb
import app.routers.global_facts as gf
import app.routers.global_facts_docs as gfd
import app.services.file_parser as fp
import pytest
from app.db import get_conn, init_db
from app.schema_sql import SCHEMA_SQL
from fastapi import UploadFile


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


# ===========================================================================
# BUG-2 · UTF-16 编码识别
# ===========================================================================


class TestUtf16Decoding:
    """UTF-16 两个端序（含/不含 BOM）必须正确解码，且不得落到 GBK 乱码分支。"""

    def test_utf16le_bom_decodes_to_chinese(self):
        payload = "第一批 设备清单\n".encode("utf-16")  # BOM + LE
        text, warnings = fp._decode_text_ex(payload)
        assert text == "第一批 设备清单\n"
        assert any("UTF-16" in w for w in warnings)
        assert not any("gbk" in w for w in warnings), "UTF-16 不得被 GBK 分支吞掉"

    def test_utf16be_bom_decodes_to_chinese(self):
        payload = b"\xfe\xff" + "第二批 设备清单\n".encode("utf-16-be")
        text, warnings = fp._decode_text_ex(payload)
        assert text == "第二批 设备清单\n"
        assert any("UTF-16" in w for w in warnings)
        assert not any("gbk" in w for w in warnings)

    def test_utf16le_without_bom_detected(self):
        payload = "Name: 设备清单\n".encode("utf-16-le")
        assert fp._detect_utf16(payload) == ("utf-16-le", "UTF-16LE（无 BOM，按字节分布推测）")
        text, warnings = fp._decode_text_ex(payload)
        assert text == "Name: 设备清单\n"
        assert any("UTF-16" in w for w in warnings)

    def test_utf16be_without_bom_detected(self):
        payload = "Name: 设备清单\n".encode("utf-16-be")
        assert fp._detect_utf16(payload) == ("utf-16-be", "UTF-16BE（无 BOM，按字节分布推测）")
        text, _warnings = fp._decode_text_ex(payload)
        assert text == "Name: 设备清单\n"

    def test_plain_utf8_not_misdetected_as_utf16(self):
        """普通无 BOM UTF-8 不得被误判为 UTF-16（否则整篇变乱码）。"""
        payload = "设备清单\n".encode("utf-8") * 20
        assert fp._detect_utf16(payload) is None
        text, warnings = fp._decode_text_ex(payload)
        assert "设备清单" in text
        assert warnings == []

    def test_gbk_path_unchanged(self):
        """原 GBK 路径不回归：正确解码，且仍带「已按 gbk 解码」告警。"""
        text, warnings = fp._decode_text_ex("中文测试\n".encode("gbk"))
        assert text == "中文测试\n"
        assert any("gbk" in w for w in warnings)
        assert not any("UTF-16" in w for w in warnings)


# ===========================================================================
# BUG-3 · 孤立 CR 告警
# ===========================================================================


class TestLoneCrWarning:
    def test_lone_cr_line_endings_restored_and_warned(self):
        """内容层修复：孤立 CR 还原为换行，多行不再折叠；同时告警留痕。"""
        payload = "第一章 工程概况\r第二章 施工部署\r第三章 进度计划".encode("utf-8")
        text, warnings = fp._decode_text_ex(payload)
        assert any("单独的 CR" in w for w in warnings)
        assert (
            text == "第一章 工程概况\n第二章 施工部署\n第三章 进度计划"
        ), "孤立 CR 必须还原为换行，目录识别/事实分段依赖多行结构"

    def test_single_lone_cr_still_restored_without_warning(self):
        """单处孤立 CR：内容层无条件还原（避免后续内容折叠），但不出告警。"""
        text, warnings = fp._decode_text_ex("a\rb\nc".encode("utf-8"))
        assert text == "a\nb\nc"
        assert not any("单独的 CR" in w for w in warnings)

    def test_mixed_crlf_and_lone_cr(self):
        """CR+LF 混排：CRLF 保持一行分隔，孤立 CR 各自成行，互不吞行。

        告警阈值要求「孤立 CR 成规模且多于 CRLF」；2:2 对半混排不构成
        CR 主导，内容已正确还原故不告警（避免误报），此处锁定该口径。
        """
        text, warnings = fp._decode_text_ex("一\r\n二\r三\r\n四\r五".encode("utf-8"))
        assert text == "一\n二\n三\n四\n五"
        assert not any("单独的 CR" in w for w in warnings)

    def test_mixed_cr_dominant_warns(self):
        """CR 主导混排（孤立 CR 多于 CRLF）：还原生效且告警留痕。"""
        text, warnings = fp._decode_text_ex("一\r\n二\r三\r四\r五".encode("utf-8"))
        assert text == "一\n二\n三\n四\n五"
        assert any("单独的 CR" in w for w in warnings)

    def test_crlf_does_not_trigger_lone_cr_warning(self):
        _text, warnings = fp._decode_text_ex("a\r\nb\r\nc".encode("utf-8"))
        assert not any("单独的 CR" in w for w in warnings)

    def test_lone_cr_not_misdetected_in_crlf_dominant_text(self):
        """CRLF 为主的文本夹带少量孤立 CR：不告警（<2 处时不触发）。"""
        _text, warnings = fp._decode_text_ex("a\r\nb\rc\r\nd".encode("utf-8"))
        assert not any("单独的 CR" in w for w in warnings)


# ===========================================================================
# G7 · 落库截断对齐完整换行
# ===========================================================================


class TestStorageTruncation:
    def test_cuts_at_last_complete_newline(self, monkeypatch):
        monkeypatch.setattr(gfd, "MAX_PARSED_CHARS", 100)
        text = ("x" * 50 + "\n") * 10
        stored, truncated = gfd._truncate_for_storage(text)
        assert truncated is True
        assert stored == text[:51]
        assert stored.endswith("\n")
        assert stored.count("\n") == 1

    def test_table_row_not_cut_in_half(self, monkeypatch):
        monkeypatch.setattr(gfd, "MAX_PARSED_CHARS", 60)
        row = "| 项目 | 数量 |\n"
        stored, truncated = gfd._truncate_for_storage(row * 20)
        assert truncated is True
        assert stored.endswith("\n")
        assert all(line.endswith("|") for line in stored.splitlines())

    def test_long_single_line_falls_back_to_hard_cut(self, monkeypatch):
        monkeypatch.setattr(gfd, "MAX_PARSED_CHARS", 100)
        stored, truncated = gfd._truncate_for_storage("y" * 500)
        assert truncated is True
        assert stored == "y" * 100

    def test_under_limit_untouched(self, monkeypatch):
        monkeypatch.setattr(gfd, "MAX_PARSED_CHARS", 100)
        stored, truncated = gfd._truncate_for_storage("短文本\n")
        assert stored == "短文本\n"
        assert truncated is False


# ===========================================================================
# BUG-1 · 同名上传单行化 + 迁移
# ===========================================================================


@pytest.fixture
async def fact_db(tmp_path, monkeypatch):
    """真实文件库（含 init_db 迁移）+ 上传目录隔离到 tmp。"""
    _appdb.DB_PATH = tmp_path / "r175.sqlite"
    await init_db()
    db = await get_conn()
    uploads = tmp_path / "uploads" / "facts"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid, _appdb.DB_PATH


async def _same_name_rows(db, pid):
    cur = await db.execute(
        "SELECT id, file_name, file_path, file_size FROM project_documents "
        "WHERE project_id=? AND file_name='same.txt'",
        (pid,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def test_sequential_same_name_keeps_single_row_with_last_content(fact_db):
    """同名二次上传：只留 1 行，id/文件均为最后一次，旧文件已清理。"""
    db, pid, _path = fact_db
    first = await gf.upload_documents(project_id=pid, files=[_upload("same.txt", b"first version")], db=db)
    first_rows = await _same_name_rows(db, pid)
    assert len(first_rows) == 1

    second_payload = b"second version longer"
    second = await gf.upload_documents(project_id=pid, files=[_upload("same.txt", second_payload)], db=db)

    assert second["replaced"] == 1
    rows = await _same_name_rows(db, pid)
    assert len(rows) == 1, "同名上传必须单行化"
    assert rows[0]["id"] == second["saved"][0]["id"], "upsert 应换用新行 id"
    assert Path(rows[0]["file_path"]).read_bytes() == second_payload
    assert not Path(first_rows[0]["file_path"]).exists(), "旧原件应在 commit 后清理"
    assert first["saved_count"] == 1


async def test_concurrent_same_name_uploads_leave_single_row(fact_db):
    """两条连接并发同名上传：最终必须只有 1 行（数据库唯一约束兜底）。"""
    _db, pid, db_path = fact_db
    conn_a = await aiosqlite.connect(db_path, timeout=10)
    conn_b = await aiosqlite.connect(db_path, timeout=10)
    conn_a.row_factory = aiosqlite.Row
    conn_b.row_factory = aiosqlite.Row
    payload_a = b"alpha"
    payload_b = b"beta payload"
    try:
        results = await asyncio.gather(
            gf.upload_documents(project_id=pid, files=[_upload("same.txt", payload_a)], db=conn_a),
            gf.upload_documents(project_id=pid, files=[_upload("same.txt", payload_b)], db=conn_b),
        )
        assert all(r.get("saved_count") == 1 for r in results)
        rows = await _same_name_rows(conn_a, pid)
        assert len(rows) == 1, f"并发同名上传产生了 {len(rows)} 行"
        stored = Path(rows[0]["file_path"]).read_bytes()
        assert stored in (payload_a, payload_b), "保留内容必须是其中之一（非半截/串行混写）"
        assert rows[0]["file_size"] == len(stored)
    finally:
        await conn_a.close()
        await conn_b.close()


async def test_migration_dedupes_legacy_rows_and_builds_unique_index():
    """旧库含 3 行同名重复档案：迁移后只留最新 1 行，且唯一索引生效、可重入。"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(SCHEMA_SQL)
        await conn.execute("INSERT INTO projects(id,name) VALUES('p1','p')")
        for doc_id, fpath in (("d1", "/old1"), ("d2", "/old2"), ("d3", "/new3")):
            await conn.execute(
                "INSERT INTO project_documents(id,project_id,file_name,file_path) " "VALUES(?,?,?,?)",
                (doc_id, "p1", "招标文件.pdf", fpath),
            )
        await conn.commit()
        cur = await conn.execute("SELECT COUNT(*) FROM project_documents WHERE project_id='p1'")
        assert (await cur.fetchone())[0] == 3

        await _appdb._migrate(conn)
        await conn.commit()

        cur = await conn.execute("SELECT id, file_path FROM project_documents WHERE project_id='p1'")
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1, "迁移必须按 (project_id, file_name) 去重"
        assert rows[0]["id"] == "d3" and rows[0]["file_path"] == "/new3", "应保留最新（rowid 最大）一行"

        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' " "AND name='uq_project_documents_pid_fname'"
        )
        assert (await cur.fetchone()) is not None, "唯一索引必须建立"

        # 唯一索引真的生效：再插同名行必须被拒绝
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO project_documents(id,project_id,file_name) " "VALUES('d4','p1','招标文件.pdf')"
            )

        # 可重入：再跑一次迁移不报错、不误删
        await _appdb._migrate(conn)
        await conn.commit()
        cur = await conn.execute("SELECT COUNT(*) FROM project_documents WHERE project_id='p1'")
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()
