"""解析提取模块 · 截断修复护栏（2026-09-30 第十四轮）

修复的两个真实缺陷：

1. **【P0 数据丢失】PDF 文本层页数上限硬编码 50**
   招标文件 / 施工组织设计常见 100~400 页，一份 300 页的招标文件只提取前 50 页，
   **后面 250 页的工程参数、清单、图纸说明全部丢失**，且不会进入目录 / 正文 /
   事实 / 导出任何一级。现改为读 ``settings.pdf_text_max_pages``（默认 500）。

2. **【P0 静默截断】截断文档被当成完整依据**
   ``generate_facts`` 的 SQL 只取 ``(file_name, parsed_markdown)``，从不读
   ``parse_truncated`` —— 而解析阶段已把「PDF 截页 / 表格截行 / 落库字数超限」
   写进该列。用户看到「提取完成」却无从得知依据是残缺的。

顺带修掉一个**恒空死分支**（见 ``TestPendingDocsNeverFires``）。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.config import settings
from app.routers import sse_handlers as sh
from app.services import file_parser as fp


# =========================================================================
# 一、PDF 页数上限：可配置 + 默认值放开
# =========================================================================
class TestPdfPageLimit:
    def test_module_constant_follows_config(self):
        """MAX_PDF_PAGES 必须来自配置（不再硬编码 50）。"""
        assert fp._resolve_pdf_max_pages() == settings.pdf_text_max_pages
        assert fp.MAX_PDF_PAGES == fp._resolve_pdf_max_pages()

    def test_default_limit_is_large_enough_for_tender_docs(self):
        """默认 500 页：覆盖常见 100~400 页招标文件。"""
        assert fp.MAX_PDF_PAGES >= 400
        assert settings.pdf_text_max_pages >= 400

    def test_aligned_with_storage_char_limit(self):
        """与 MAX_PARSED_CHARS 对齐：约 800 字/页 × 500 页 ≈ 400000 字。"""
        from app.routers.global_facts import MAX_PARSED_CHARS
        assert MAX_PARSED_CHARS >= 400_000
        # 不允许出现「解析上限页数 × 每页估字 < 落库上限」这种二次浪费
        assert fp.MAX_PDF_PAGES * 800 >= MAX_PARSED_CHARS

    @pytest.mark.parametrize("bad", [0, -1, None, "abc", 3.7])
    def test_invalid_config_falls_back_to_50(self, bad, monkeypatch):
        """非法配置必须回落到旧基线 50，绝不能返回 0。

        返回 0 会让 ``doc.pages(0, 0)`` 一页都不解析、且因「页数 > 上限」
        不成立而**不产生任何截断告警** —— 用户拿到空文档却看不到原因。
        """
        monkeypatch.setattr(settings, "pdf_text_max_pages", bad)
        got = fp._resolve_pdf_max_pages()
        assert got == 50, bad
        assert got > 0

    def test_config_is_overridable_back_to_small(self, monkeypatch):
        """显式设小可恢复「只取前 N 页」行为（保留旧语义的能力）。"""
        monkeypatch.setattr(settings, "pdf_text_max_pages", 10)
        assert fp._resolve_pdf_max_pages() == 10

    def test_module_global_stays_monkeypatchable(self, monkeypatch):
        """既有 ``monkeypatch.setattr(fp, "MAX_PDF_PAGES", N)`` 必须照常生效。"""
        monkeypatch.setattr(fp, "MAX_PDF_PAGES", 3)
        assert fp.MAX_PDF_PAGES == 3

    def test_all_call_sites_read_module_global(self):
        """调用点必须按模块全局读取（否则配置/单测都改不动）。"""
        src = inspect.getsource(fp)
        assert src.count("MAX_PDF_PAGES") >= 5
        assert "import MAX_PDF_PAGES" not in src
# =========================================================================
# 二、截断诊断读取（三层降级）
# =========================================================================
def _make_db(tmp_path, rows, *, cols="both"):
    """建一个只含 project_documents 的最小内存库。

    ``cols``：``"both"``（两诊断列都在）/ ``"warn_only"``（旧库，只有
    ``parse_warnings``）/ ``"none"``（两列都没有）。
    """
    import sqlite3
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE project_documents ("
                 "id TEXT PRIMARY KEY, project_id TEXT, file_name TEXT,"
                 "parsed_markdown TEXT, created_at TEXT DEFAULT '')")
    if cols in ("both", "warn_only"):
        conn.execute("ALTER TABLE project_documents "
                     "ADD COLUMN parse_warnings TEXT DEFAULT ''")
    if cols == "both":
        conn.execute("ALTER TABLE project_documents "
                     "ADD COLUMN parse_truncated INTEGER DEFAULT 0")
    for r in rows:
        vals = list(r[:4])
        if cols == "both":
            vals += list(r[4:6]) if len(r) >= 6 else ["", 0]
            conn.execute(
                "INSERT INTO project_documents(id,project_id,file_name,"
                "parsed_markdown,parse_warnings,parse_truncated) "
                "VALUES(?,?,?,?,?,?)", tuple(vals))
        elif cols == "warn_only":
            vals.append(r[4] if len(r) >= 5 else "")
            conn.execute(
                "INSERT INTO project_documents(id,project_id,file_name,"
                "parsed_markdown,parse_warnings) VALUES(?,?,?,?,?)",
                tuple(vals))
        else:
            conn.execute(
                "INSERT INTO project_documents(id,project_id,file_name,parsed_markdown)"
                " VALUES(?,?,?,?)", tuple(vals))
    conn.commit()
    return conn


class _AsyncShim:
    """把同步 sqlite3.Connection 包成 aiosqlite 风格的 await 接口。

    真实连接是 aiosqlite：``await db.execute(...)`` 返回一个游标，且
    ``await cur.fetchall()`` 可 await。这里用一层薄壳复刻该契约。
    """

    class _Cur:
        def __init__(self, cur):
            self._cur = cur

        async def fetchall(self):
            return self._cur.fetchall()

        async def fetchone(self):
            return self._cur.fetchone()

    def __init__(self, conn):
        self._c = conn

    async def execute(self, sql, params=()):
        return self._Cur(self._c.execute(sql, params))

    def close(self):
        self._c.close()


class TestTruncationSurfacing:
    def test_reads_parse_truncated_column(self, tmp_path):
        rows = [
            ("1", "p1", "完整.pdf", "正文A" * 100, "", 0),
            ("2", "p1", "截断.pdf", "正文B" * 100, "PDF 共 300 页，仅解析前 50 页", 1),
        ]
        db = _AsyncShim(_make_db(tmp_path, rows))
        names, bodies, trunc = asyncio.run(sh._load_facts_source_docs(db, "p1"))
        assert names == ["完整.pdf", "截断.pdf"]
        assert bodies[0].startswith("正文A")
        assert trunc == ["截断.pdf"]

    def test_falls_back_to_parse_warnings_when_no_truncated_col(self, tmp_path):
        """旧库只有 parse_warnings 列 → 按告警文本含「截断」兜底判定。"""
        rows = [
            ("1", "p1", "截断.pdf", "正文B", "已截断至上限", 0),
            ("2", "p1", "正常.pdf", "正文A", "", 0),
        ]
        db = _AsyncShim(_make_db(tmp_path, rows, cols="warn_only"))
        names, _b, trunc = asyncio.run(sh._load_facts_source_docs(db, "p1"))
        assert names == ["截断.pdf", "正常.pdf"]
        assert trunc == ["截断.pdf"]

    def test_falls_back_when_diag_cols_absent(self, tmp_path):
        """两列都没有 → 只取基础两列（与引入前逐字一致，不报错）。"""
        rows = [("1", "p1", "a.pdf", "正文A"), ("2", "p1", "empty.pdf", "")]
        db = _AsyncShim(_make_db(tmp_path, rows, cols="none"))
        names, bodies, trunc = asyncio.run(sh._load_facts_source_docs(db, "p1"))
        assert names == ["a.pdf"]          # 空正文被过滤
        assert bodies == ["正文A"]
        assert trunc == []

    def test_query_failure_degrades_not_raises(self, tmp_path):
        """读失败必须降级为空而非抛异常（绝不阻断提取）。"""
        class Boom:
            async def execute(self, *a, **k):
                raise RuntimeError("db down")
        assert asyncio.run(sh._load_facts_source_docs(Boom(), "p1")) == ([], [], [])

    def test_empty_project_returns_empty(self, tmp_path):
        db = _AsyncShim(_make_db(tmp_path, []))
        assert asyncio.run(sh._load_facts_source_docs(db, "p1")) == ([], [], [])
# =========================================================================
# 三、「待解析文档」告警原本恒不触发（死分支）
# =========================================================================
class TestPendingDocsNeverFires:
    def test_pending_docs_actually_detected(self, tmp_path):
        """旧实现用「已过滤掉空正文」的结果集算 pending → 恒为空。"""
        rows = [("1", "p1", "已解析.pdf", "正文"), ("2", "p1", "未解析.pdf", "")]
        db = _AsyncShim(_make_db(tmp_path, rows))
        has_any, pending = asyncio.run(sh._count_docs_pending_parse(db, "p1"))
        assert has_any is True
        assert pending == ["未解析.pdf"]

    def test_has_any_doc_true_even_when_all_unparsed(self, tmp_path):
        """全部未解析时 ``has_any_doc`` 必须为 True。

        否则提示会说「项目下没有已上传的资料文档」——而用户明明传了文件。
        """
        rows = [("1", "p1", "a.pdf", ""), ("2", "p1", "b.pdf", None)]
        db = _AsyncShim(_make_db(tmp_path, rows))
        has_any, pending = asyncio.run(sh._count_docs_pending_parse(db, "p1"))
        assert has_any is True
        assert pending == ["a.pdf", "b.pdf"]

    def test_no_docs(self, tmp_path):
        db = _AsyncShim(_make_db(tmp_path, []))
        assert asyncio.run(sh._count_docs_pending_parse(db, "p1")) == (False, [])

    def test_query_failure_degrades(self):
        class Boom:
            async def execute(self, *a, **k):
                raise RuntimeError("db down")
        assert asyncio.run(sh._count_docs_pending_parse(Boom(), "p1")) == (False, [])


# =========================================================================
# 四、SSE 链路接线与静态护栏
# =========================================================================
class TestSseWiring:
    def test_generate_facts_uses_new_helpers(self):
        src = inspect.getsource(sh.generate_facts)
        assert "_load_facts_source_docs" in src
        assert "_count_docs_pending_parse" in src

    def test_truncation_warning_event_present(self):
        """截断必须以 SSE warning 事件显式告知（不能静默）。"""
        src = inspect.getsource(sh.generate_facts)
        assert "truncated_docs" in src
        assert "解析不完整" in src
        assert '"event": "warning"' in src

    def test_old_broken_pending_line_removed(self):
        """旧的恒空表达式必须已删除。"""
        src = inspect.getsource(sh.generate_facts)
        assert "if False else []" not in src

    def test_source_files_and_text_stay_paired(self):
        """文件名与正文必须成对（防止 zip 错位导致来源标注对不上）。"""
        src = inspect.getsource(sh.generate_facts)
        assert "zip(parsed_file_names, parsed_bodies)" in src

    def test_no_direct_raw_query_in_generate_facts(self):
        """源文档读取收敛到单一 helper（避免 SQL 在多处漂移）。"""
        src = inspect.getsource(sh.generate_facts)
        assert src.count("SELECT file_name") == 0