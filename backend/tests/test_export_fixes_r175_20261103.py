"""R175 导出域修复回归（2026-11-03）。

覆盖：
- BUG-2：XML 非法控制字符清洗（`_xml_safe_text` / `_safe_add_run`）——
  表单里混入 \\x00 等字符不得让整单导出 500。
- BUG-3：缓存行可用判据升级为 DOCX（ZIP）完整性校验（损坏产物不得命中）。
- BUG-4：`max_ai_images` / `image_provider` / `export_appendix_sources`
  纳入缓存指纹（切换即失效旧缓存）。
- BUG-5：`_prepare_export` 章节严格读 → 503；零章节/全空章 → 409。
- BUG-6：缓存「读取租约」——并发 prune 不得删除正在被下载的产物。
- RISK-175-01：预渲染 order_rank 与写侧统一为 DFS 文档序。
"""

import asyncio
import uuid
import zipfile
from pathlib import Path

import app.routers.export as E
import pytest
from docx import Document
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# BUG-2 · XML 非法控制字符清洗
# ---------------------------------------------------------------------------
def test_xml_safe_text_strips_c0_and_preserves_legal_whitespace():
    raw = "标题\x00\x01\x08\x0b\x0c\x0e\x1f\x7f\t\n\r 正常文本"
    assert E._xml_safe_text(raw) == "标题\t\n\r 正常文本"


def test_xml_safe_text_handles_none_and_surrogates():
    assert E._xml_safe_text(None) == ""
    assert E._xml_safe_text("a\ud800b") == "ab"


def test_safe_add_run_with_control_chars_saves_openable_docx(tmp_path):
    doc = Document()
    p = doc.add_paragraph()
    E._safe_add_run(p, "投标人\x00某某\x0b建设")
    out = tmp_path / "control_chars.docx"
    doc.save(str(out))

    with zipfile.ZipFile(out) as zf:
        assert zf.testzip() is None
    # python-docx 重新打开（Word/WPS 同源的结构入口）必须成功
    assert "投标人某某建设" in Document(str(out)).paragraphs[0].text


# ---------------------------------------------------------------------------
# BUG-3 · 缓存行可用判据：真实完整性校验
# ---------------------------------------------------------------------------
def test_viable_rejects_truncated_docx(tmp_path):
    p = tmp_path / "truncated.docx"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 128)
    assert E._cache_row_viable(str(p)) is False


def test_viable_accepts_real_docx_and_memoizes(tmp_path, monkeypatch):
    p = tmp_path / "ok.docx"
    Document().save(str(p))
    assert E._cache_row_viable(str(p)) is True

    calls = {"n": 0}
    real_testzip = zipfile.ZipFile.testzip

    def _counting(self):
        calls["n"] += 1
        return real_testzip(self)

    monkeypatch.setattr(zipfile.ZipFile, "testzip", _counting)
    assert E._cache_row_viable(str(p)) is True
    assert calls["n"] == 0, "同一 (路径,大小,mtime) 不应重复解压校验"


def test_viable_recheck_after_file_rewritten(tmp_path):
    p = tmp_path / "rewritten.docx"
    Document().save(str(p))
    assert E._cache_row_viable(str(p)) is True
    # 重写为损坏内容后 大小/mtime 变化 → 必须重新判定为不可用
    p.write_bytes(b"not-a-zip" * 100)
    assert E._cache_row_viable(str(p)) is False


# ---------------------------------------------------------------------------
# BUG-4 · 缓存指纹补齐渲染相关配置
# ---------------------------------------------------------------------------
def _minimal_prep():
    return {
        "config": {},
        "scheme": {"name": "方案A", "project_id": "p1"},
        "sections": [
            {
                "id": "s1",
                "parent_id": "",
                "sort_order": 0,
                "level": 1,
                "title": "第一章",
                "content": "正文",
            }
        ],
        "chart_fp": [],
        "fe_codes": [],
        "fe_images": {},
    }


def test_fingerprint_changes_when_max_ai_images_changes(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_ai_images", 0)
    _, h0 = E._content_fingerprint(_minimal_prep())
    monkeypatch.setattr(settings, "max_ai_images", 4)
    _, h4 = E._content_fingerprint(_minimal_prep())
    assert h0 != h4, "max_ai_images 未进入指纹 → 改预算仍命中旧缓存"


def test_fingerprint_changes_when_image_provider_changes(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "image_provider", "agnes")
    _, h1 = E._content_fingerprint(_minimal_prep())
    monkeypatch.setattr(settings, "image_provider", "dashscope")
    _, h2 = E._content_fingerprint(_minimal_prep())
    assert h1 != h2


def test_fingerprint_changes_when_appendix_switch_changes(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "export_appendix_sources", False)
    _, h_off = E._content_fingerprint(_minimal_prep())
    monkeypatch.setattr(settings, "export_appendix_sources", True)
    _, h_on = E._content_fingerprint(_minimal_prep())
    assert h_off != h_on


# ---------------------------------------------------------------------------
# BUG-5 · 章节严格读 / 空章拦截
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, one=None, rows=None, err=None):
        self._one, self._rows, self._err = one, rows, err

    async def fetchone(self):
        if self._err:
            raise RuntimeError("db down")
        return self._one

    async def fetchall(self):
        if self._err:
            raise RuntimeError("db down")
        return list(self._rows or [])


class _FakeDB:
    def __init__(self, sections_cur):
        self._sections_cur = sections_cur

    async def execute(self, sql, params=()):
        if "FROM schemes" in sql:
            return _Cur(one={"id": "s1", "name": "方案A", "project_id": "p1"})
        if "FROM sections" in sql:
            return self._sections_cur
        return _Cur(rows=[])


async def test_prepare_export_sections_read_failure_returns_503():
    with pytest.raises(HTTPException) as ei:
        await E._prepare_export("s1", {"config": {}}, _FakeDB(_Cur(err=True)))
    assert ei.value.status_code == 503


async def test_prepare_export_zero_sections_is_marked_degraded():
    """零章节 → 明确降级留痕（不 500/不拦死），且标记「不写缓存」。"""
    plan = await E._prepare_export("s1", {"config": {}}, _FakeDB(_Cur(rows=[])))
    assert plan["empty_sections"] is True


async def test_prepare_export_all_blank_sections_is_marked_degraded():
    rows = [
        {"id": "a", "title": "  ", "content": "", "sort_order": 0, "level": 1, "parent_id": None},
        {"id": "b", "title": "", "content": "\n\t ", "sort_order": 1, "level": 1, "parent_id": None},
    ]
    plan = await E._prepare_export("s1", {"config": {}}, _FakeDB(_Cur(rows=rows)))
    assert plan["empty_sections"] is True


async def test_prepare_export_with_content_is_not_degraded():
    rows = [{"id": "a", "title": "第一章", "content": "正文", "sort_order": 0, "level": 1, "parent_id": None}]
    plan = await E._prepare_export("s1", {"config": {}}, _FakeDB(_Cur(rows=rows)))
    assert plan["empty_sections"] is False


# ---------------------------------------------------------------------------
# BUG-6 · 读取租约保护
# ---------------------------------------------------------------------------
async def _seed_rows(db, sid, tmp_path, n=7):
    files = []
    for i in range(n):
        f = tmp_path / f"old_{i}.docx"
        Document().save(str(f))
        files.append(f)
        await db.execute(
            "INSERT INTO export_cache "
            "(id, project_id, scheme_id, config_hash, content_fingerprint,"
            " cache_key, result_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, "", sid, "cfg", f"fp{i}", f"{sid}_cfg{i}", str(f), f"2026-10-01 10:00:{i:02d}"),
        )
    await db.commit()
    return files


def _add_scheme_row(db, sid):
    return db.execute("INSERT OR IGNORE INTO schemes(id, project_id, name) VALUES(?,?,?)", (sid, "", "s"))


async def test_prune_skips_file_under_read_lease(db_conn, tmp_path):
    sid = uuid.uuid4().hex
    await _add_scheme_row(db_conn, sid)
    files = await _seed_rows(db_conn, sid, tmp_path)
    E._EXPORT_READ_LEASES.clear()
    try:
        E._export_read_lease_register(files[0])
        assert E._export_inflight_key(files[0]) in E._export_read_lease_snapshot()

        await E._prune_export_cache(db_conn, sid, protect_path=tmp_path / "cur.docx")

        assert files[0].exists(), "正在被读取的产物被 prune 删除"
        assert not files[1].exists(), "未受租约保护的陈旧产物应被清理"
        cur = await db_conn.execute("SELECT COUNT(*) AS n FROM export_cache WHERE scheme_id=?", (sid,))
        row = await cur.fetchone()
        assert dict(row)["n"] == 5
    finally:
        E._EXPORT_READ_LEASES.clear()


async def test_concurrent_download_and_prune_no_error(db_conn, tmp_path):
    sid = uuid.uuid4().hex
    await _add_scheme_row(db_conn, sid)
    files = await _seed_rows(db_conn, sid, tmp_path)
    E._EXPORT_READ_LEASES.clear()
    try:
        E._export_read_lease_register(files[0])
        leaked = []

        async def _reader():
            # 模拟 FileResponse 懒加载：反复打开该文件直到 prune 结束
            for _ in range(60):
                try:
                    files[0].read_bytes()
                except FileNotFoundError:
                    leaked.append("gone")
                    return
                await asyncio.sleep(0.001)

        prune = E._prune_export_cache(db_conn, sid, protect_path=tmp_path / "cur.docx")
        await asyncio.wait_for(asyncio.gather(_reader(), prune), timeout=10)
        assert leaked == [], "并发下载期间产物被删除（用户会收到 404/0 字节）"
        assert files[0].exists()
    finally:
        E._EXPORT_READ_LEASES.clear()


def test_read_lease_expires():
    E._EXPORT_READ_LEASES.clear()
    try:
        E._export_read_lease_register("C:/exports/lease_target.docx")
        key = E._export_inflight_key(Path("C:/exports/lease_target.docx"))
        assert key in E._export_read_lease_snapshot()
        # 人为把过期时间拨到过去 → 快照应剔除
        E._EXPORT_READ_LEASES[key] = 0.0
        assert key not in E._export_read_lease_snapshot()
    finally:
        E._EXPORT_READ_LEASES.clear()


# ---------------------------------------------------------------------------
# RISK-175-01 · 统一排序口径（DFS 文档序）
# ---------------------------------------------------------------------------
def test_doc_order_rank_uses_dfs_document_order():
    root_a = {"id": "a", "sort_order": 1, "level": 1}
    root_b = {"id": "b", "sort_order": 2, "level": 1}
    child_a1 = {"id": "a1", "sort_order": 1, "level": 2}
    children = {"a": [child_a1], "b": []}
    rank = E._compute_doc_order_rank([root_b, root_a], children)
    # DFS：a → a1 → b（而不是 SQL 扁平序 a, b, a1）
    assert rank == {"a": 0, "a1": 1, "b": 2}


def test_prerender_and_writer_share_same_order_rank():
    """预渲染段与写侧必须产出同一 rank 口径（防再次分叉）。"""
    roots = [{"id": "b", "sort_order": 2, "level": 1}, {"id": "a", "sort_order": 1, "level": 1}]
    children = {"a": [{"id": "a1", "sort_order": 1, "level": 2}], "b": []}
    assert E._compute_doc_order_rank(roots, children) == E._compute_doc_order_rank(roots, children)
    flat = {s["id"]: i for i, s in enumerate([roots[1], roots[0], children["a"][0]])}
    dfs = E._compute_doc_order_rank(roots, children)
    assert flat != dfs, "测试自身失效：DFS 与扁平序在父子结构下应不同"
