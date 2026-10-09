"""解析提取模块 R13 收口第二批（2026-10-08）：upload_outline 全路由 + parse_document 首查。

2026-10-05 的 R41 收口覆盖了 global_facts 文档链与 doc_pipeline，但本模块仍有两类
漏网（探针核对）：

  ① `global_facts.parse_document` 的【首查】（按 id 读文档档案）未判空 ——
     同函数锁内二次复核已回 503（R41），首查命中 None 却 AttributeError → 500，
     同一端点两条读路径口径分叉。既有护栏
     `test_parse_document_none_cursor_returns_503` 的谓词只匹配二次复核的 SQL
     （`SELECT parsed_markdown FROM ...`），覆盖不到首查（`SELECT file_name,
     file_path, ...`）—— 本文件补上。
  ② `upload_outline.py`（目录识别上传/保存链）整条路由 R13 零防护：
     读路径 None → 500；写路径「假成功」（INSERT 未生效仍回 id、save-as-library
     未落库仍回 ok=True）；save-as-outline 章节预取若降级成空列表会跳过标题
     匹配 → 重复插入两套目录（必须 503 中止，不得 fail-soft）。

手法与 R41 相同：代理连接按 SQL 谓词把 execute/executemany 重定向到 None，
并统一以 `proxy.hits >= 1` 做变异守卫（本用例必须真的命中 None 分支）。

✅ 深化（同日 · 零行/少行写）：None 只回答「语句根本没执行」；本文件另以
`_RowsDb`（假游标伪造 rowcount）覆盖第二层 —— 「执行了但一行没落到 / 批量
少落几行」（预取与写链之间目标记录被并发删除）。`_r13_write` 在任何提交前
按点数中止：状态回写 0 行 → 404，章节结构少行 → 409，写入未完整 → 503；
`chart_predictions` 的级联 DELETE 命中 0 行属正常情况，**不得**做行数校验
（G9 行为护栏锁定该口径）。
"""
from __future__ import annotations

import io
import json
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import app.routers.upload_outline as uo
import pytest
from app.db import get_conn, init_db
from fastapi import HTTPException
from starlette.datastructures import UploadFile

# 与 test_import_parse_r13_closeout_20261005._NullDb 同款代理（测试文件间
# 不互相 import，避免夹具耦合；本文件自带一份）。
class _NullDb:
    def __init__(self, conn, predicate):
        self._c = conn
        self._p = predicate
        self.hits = 0

    async def execute(self, sql, params=()):
        if self._p(sql):
            self.hits += 1
            return None
        return await self._c.execute(sql, params)

    async def executemany(self, sql, seq):
        if self._p(sql):
            self.hits += 1
            return None
        return await self._c.executemany(sql, seq)

    async def commit(self):
        await self._c.commit()

    async def rollback(self):
        await self._c.rollback()


# ===========================================================================
# 零行/少行写代理（2026-10-08 深化）：游标**非 None**但 rowcount 与预期不符 ——
# _NullDb 的 None 分支拦不住这类「执行了但一行没落到 / 批量少落几行」，
# 需要伪造一个只控制 rowcount 的假游标。
# ===========================================================================

class _FakeCur:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _RowsDb:
    """predicate 命中的写语句不真执行，改回携指定 rowcount 的假游标。

    rc 可为 int，或 callable(seq_or_params) -> int（批量写少落 N 行场景，
    如 lambda seq: len(seq) - 1）。
    """

    def __init__(self, conn, predicate, rc):
        self._c = conn
        self._p = predicate
        self._rc = rc
        self.hits = 0

    def _rows(self, arg):
        return self._rc(arg) if callable(self._rc) else self._rc

    async def execute(self, sql, params=()):
        if self._p(sql):
            self.hits += 1
            return _FakeCur(self._rows(params))
        return await self._c.execute(sql, params)

    async def executemany(self, sql, seq):
        if self._p(sql):
            self.hits += 1
            return _FakeCur(self._rows(seq))
        return await self._c.executemany(sql, seq)

    async def commit(self):
        await self._c.commit()

    async def rollback(self):
        await self._c.rollback()


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


# 规则法可识别的六章大纲（不触发 AI 兜底：_outline_seems_valid=True）
_OUTLINE_TXT = ("\n".join([
    "第一章 工程概况", "第二章 编制依据", "第三章 施工部署",
    "第四章 主要施工方法", "第五章 安全保证措施", "第六章 环保措施",
]).encode("utf-8"))


async def _seed_scheme(db, pid: str | None = None, sid: str | None = None):
    pid = pid or uuid.uuid4().hex
    sid = sid or uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    return pid, sid


async def _seed_upload(db, rid: str | None = None, status: str = "parsed",
                       project_id: str = "") -> str:
    rid = rid or uuid.uuid4().hex
    await db.execute(
        "INSERT INTO uploaded_outlines (id, project_id, file_name, status)"
        " VALUES (?,?,?,?)", (rid, project_id, "x.txt", status))
    await db.commit()
    return rid


async def _count(db, sql: str, params=()) -> int:
    cur = await db.execute(sql, params)
    return int((await cur.fetchone())[0])


# ===========================================================================
# A · global_facts.parse_document 首查判空（R41 漏改点）
# ===========================================================================

@pytest.fixture
async def gf_ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "r13b2.sqlite"
    await init_db()
    db = await get_conn()
    uploads = tmp_path / "uploads" / "facts"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    yield db
    await db.close()


class TestParseDocumentFirstQueryGuard:
    async def test_first_query_none_returns_503(self, gf_ctx):
        db = gf_ctx
        pid, sid = await _seed_scheme(db)
        await gf.upload_documents(
            scheme_id=sid, project_id="",
            files=[_upload("doc.txt", "工程名称：某某产业园".encode("utf-8"))],
            db=db)
        cur = await db.execute(
            "SELECT id FROM project_documents WHERE project_id=?", (pid,))
        doc_id = (await cur.fetchone())["id"]

        # 谓词只打首查（R41 已护栏的是锁内复核 `SELECT parsed_markdown ...`）
        proxy = _NullDb(db, lambda s: s.startswith(
            "SELECT file_name, file_path, parsed_markdown"))
        with pytest.raises(HTTPException) as e:
            await gf.parse_document(doc_id, db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1, "变异守卫：必须真的命中首查 None 分支"

    async def test_missing_doc_still_404_on_real_db(self, gf_ctx):
        """护栏不得吞掉真实 404：文档不存在仍回「文档不存在」。"""
        with pytest.raises(HTTPException) as e:
            await gf.parse_document("no-such-doc", db=gf_ctx)
        assert e.value.status_code == 404


# ===========================================================================
# B · /upload-outline/parse：读检查 + 记录 INSERT 假成功
# ===========================================================================

class TestParseOutlineGuards:
    async def test_project_check_none_returns_503(self, db_conn):
        proxy = _NullDb(db_conn, lambda s: "SELECT id FROM projects" in s)
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("大纲.txt", _OUTLINE_TXT),
                                   scheme_name="", reorganize=False,
                                   project_id=uuid.uuid4().hex, db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1

    async def test_record_insert_none_returns_503_and_no_row(self, db_conn):
        """INSERT 未生效必须 503：旧行为回 {"id": rid} 假成功，后续保存必 404。"""
        pid, _sid = await _seed_scheme(db_conn)
        proxy = _NullDb(
            db_conn,
            lambda s: s.lstrip().upper().startswith("INSERT INTO UPLOADED_OUTLINES"))
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("大纲.txt", _OUTLINE_TXT),
                                   scheme_name="", reorganize=False,
                                   project_id=pid, db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(db_conn,
                            "SELECT COUNT(*) FROM uploaded_outlines") == 0, \
            "响应没有回 id 的前提是库里确实没有半截记录"

    async def test_happy_path_persists_project_link(self, db_conn):
        """正常链路回归：识别记录落库且 project_id 归属链在位（2026-10-03 链路线）。"""
        pid, _sid = await _seed_scheme(db_conn)
        res = await uo.parse_outline(
            file=_upload("大纲.txt", _OUTLINE_TXT),
            scheme_name="", reorganize=False, project_id=pid, db=db_conn)
        assert res["id"] and res["outline"]
        cur = await db_conn.execute(
            "SELECT project_id, status FROM uploaded_outlines WHERE id=?",
            (res["id"],))
        row = await cur.fetchone()
        assert row["project_id"] == pid
        assert row["status"] == "parsed"


# ===========================================================================
# C · save-as-outline：三处读 + 写链（DELETE 级联 / executemany / 状态回写）
# ===========================================================================

class TestSaveAsOutlineGuards:
    async def test_upload_check_none_returns_503_not_404(self, db_conn):
        """读失败不得误报「上传记录不存在」（404 = 数据已丢，503 = 可重试）。"""
        rid = await _seed_upload(db_conn)
        proxy = _NullDb(
            db_conn, lambda s: s.startswith("SELECT id FROM uploaded_outlines"))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": uuid.uuid4().hex,
                      "outline": [{"title": "第一章 概况", "children": []}]},
                db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1

    async def test_scheme_check_none_returns_503(self, db_conn):
        rid = await _seed_upload(db_conn)
        _pid, sid = await _seed_scheme(db_conn)
        proxy = _NullDb(
            db_conn, lambda s: s.startswith("SELECT project_id FROM schemes"))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "第一章 概况", "children": []}]},
                db=proxy)
        assert e.value.status_code == 503

    async def test_sections_prefetch_none_returns_503_no_duplicates(self, db_conn):
        """章节预取失败必须中止 —— 降级空列表会跳过标题匹配、重复插入两套目录。"""
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('a1',?,?,'第一章 概况','旧正文',1,0)",
            (sid, pid))
        await db_conn.commit()
        proxy = _NullDb(
            db_conn, lambda s: "FROM sections WHERE scheme_id=?" in s
            and s.lstrip().upper().startswith("SELECT"))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "第一章 概况", "children": []}]},
                db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,)) == 1

    async def test_cascade_delete_none_aborts_and_rolls_back(self, db_conn):
        """级联 DELETE 未生效 → 503；未提交写链回滚后旧章节原样保留。"""
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('old',?,?,'旧章','正文',1,0)", (sid, pid))
        await db_conn.commit()
        proxy = _NullDb(
            db_conn, lambda s: s.lstrip().upper().startswith("DELETE FROM SECTIONS"))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "全新章节", "children": []}]},
                db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE id='old'") == 1, \
            "中止后不得留下半套重建（旧章节仍在、新章节未提交）"
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,)) == 1

    async def test_scheme_status_update_none_aborts(self, db_conn):
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        proxy = _NullDb(
            db_conn, lambda s: s.startswith("UPDATE schemes SET outline_source"))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "第一章 概况", "children": []}]},
                db=proxy)
        assert e.value.status_code == 503
        await db_conn.rollback()
        cur = await db_conn.execute(
            "SELECT outline_source, status FROM schemes WHERE id=?", (sid,))
        row = await cur.fetchone()
        assert (row["outline_source"] or "") != "上传识别", "回写未生效不得提交半截"

    async def test_happy_path_preserves_content_and_links(self, db_conn):
        """正常链路回归：标题匹配保留正文 + 归属链回写（scheme_id/project_id/status）。"""
        pid, sid = await _seed_scheme(db_conn)
        rid = await _seed_upload(db_conn, project_id="")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('a1',?,?,'工程概况','保留正文',1,0)",
            (sid, pid))
        await db_conn.commit()
        res = await uo.save_as_outline(
            rid, {"scheme_id": sid,
                  "outline": [{"title": "工程概况", "children": []}]}, db=db_conn)
        assert res["ok"] is True
        assert res.get("preserved_content") == 1
        cur = await db_conn.execute(
            "SELECT scheme_id, project_id, status FROM uploaded_outlines WHERE id=?",
            (rid,))
        row = await cur.fetchone()
        assert row["scheme_id"] == sid and row["project_id"] == pid
        assert row["status"] == "saved"
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE id='a1'")
        assert (await cur.fetchone())["content"] == "保留正文"


# ===========================================================================
# D · save-as-library：记录检查 / INSERT 假成功 / 状态回写
# ===========================================================================

class TestSaveAsLibraryGuards:
    _BODY = {"name": "上传识别目录",
             "outline": [{"title": "第一章 概况", "children": []}]}

    async def test_insert_none_returns_503_and_no_row(self, db_conn):
        rid = await _seed_upload(db_conn)
        proxy = _NullDb(
            db_conn,
            lambda s: s.lstrip().upper().startswith("INSERT INTO OUTLINE_LIBRARY"))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_library(rid, dict(self._BODY), db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(db_conn,
                            "SELECT COUNT(*) FROM outline_library") == 0, \
            "旧行为回 {\"ok\": true} 但库里没有行（假成功），现在不得提交"

    async def test_status_update_none_aborts(self, db_conn):
        rid = await _seed_upload(db_conn)
        proxy = _NullDb(
            db_conn,
            lambda s: s.startswith("UPDATE uploaded_outlines SET status='library_saved'"))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_library(rid, dict(self._BODY), db=proxy)
        assert e.value.status_code == 503
        await db_conn.rollback()
        cur = await db_conn.execute(
            "SELECT status FROM uploaded_outlines WHERE id=?", (rid,))
        assert (await cur.fetchone())["status"] == "parsed"

    async def test_happy_path(self, db_conn):
        rid = await _seed_upload(db_conn)
        res = await uo.save_as_library(rid, dict(self._BODY), db=db_conn)
        assert res["ok"] is True
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM outline_library WHERE id=?",
            (res["id"],)) == 1
        cur = await db_conn.execute(
            "SELECT status FROM uploaded_outlines WHERE id=?", (rid,))
        assert (await cur.fetchone())["status"] == "library_saved"


# ===========================================================================
# E · 端到端链路：parse → save-as-outline → save-as-library（同一记录贯穿）
# ===========================================================================

class TestUploadOutlineChain:
    async def test_full_chain_single_record(self, db_conn):
        pid, sid = await _seed_scheme(db_conn)
        parsed = await uo.parse_outline(
            file=_upload("大纲.txt", _OUTLINE_TXT),
            scheme_name="", reorganize=False, project_id=pid, db=db_conn)
        outline = parsed["outline"]
        saved = await uo.save_as_outline(
            parsed["id"], {"scheme_id": sid, "outline": outline}, db=db_conn)
        assert saved["ok"] is True and saved["count"] >= 5
        lib = await uo.save_as_library(
            parsed["id"], {"name": "链测目录", "outline": outline}, db=db_conn)
        assert lib["ok"] is True
        # 上传记录 → sections（章数与识别一致）
        n_secs = await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,))
        def _flat(nodes):
            total = 0
            for n in nodes:
                total += 1 + _flat(n.get("children") or [])
            return total
        assert n_secs == _flat(outline)
        # 上传记录 → outline_library（JSON 可回解且非空）
        cur = await db_conn.execute(
            "SELECT outline_json FROM outline_library WHERE id=?", (lib["id"],))
        assert json.loads((await cur.fetchone())["outline_json"])
        # 跨表归属链闭环（status 演进：parsed → saved → library_saved，
        # save-as-library 在后的调用会覆盖状态列，属既有语义）
        cur = await db_conn.execute(
            "SELECT project_id, scheme_id, status FROM uploaded_outlines WHERE id=?",
            (parsed["id"],))
        row = await cur.fetchone()
        assert (row["project_id"], row["scheme_id"], row["status"]) == (
            pid, sid, "library_saved")


# ===========================================================================
# F · 字段错位回归：sections 预取必须携带 level（三层索引层级维度在位）
# ===========================================================================

class TestSectionsLevelFieldWired:
    async def test_prefetch_selects_level_column(self):
        """结构桶 by_parent_level/by_level 依赖 er["level"]；SELECT 漏列会静默
        把全部旧章节当一级节（2026-10-04 三层索引的层级维度失效）。"""
        import inspect
        src = inspect.getsource(uo.save_as_outline)
        assert 'parent_id, level"' in src, (
            "sections 预取 SELECT 必须包含 level 列（与下方 er.get(\"level\") 消费口径对齐）")

    async def test_same_title_different_level_prefers_same_level(self, db_conn):
        """同名不同层级的旧章节：层级桶修复后，新层级 2 的同名节优先消费
        旧层级 2 的行（旧实现所有旧行都被记成层级 1，只能靠标题桶顺序命中）。"""
        pid, sid = await _seed_scheme(db_conn)
        rid = await _seed_upload(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " content, level, sort_order) VALUES ('lvl1',?,?,'','第一章',null,1,0)",
            (sid, pid))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " content, level, sort_order) VALUES ('lvl2',?,?,'lvl1','概况',"
            "'层级2正文',2,1)", (sid, pid))
        await db_conn.commit()
        res = await uo.save_as_outline(
            rid, {"scheme_id": sid, "outline": [
                {"title": "第一章", "children": [{"title": "概况", "children": []}]}]},
            db=db_conn)
        assert res.get("preserved_content") == 2
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE id='lvl2'")
        assert (await cur.fetchone())["content"] == "层级2正文"


# ===========================================================================
# G · 零行/少行写检测（_r13_write 第二层，2026-10-08 深化）
#    游标非 None 但 rowcount ≠ 预期 —— 预取后被并发删除 / 批量写少落行。
# ===========================================================================

class TestR13WriteHelperContract:
    """_r13_write 判据单元：None 与零行分流、精确点数、通过时返回行数。"""

    def test_none_cursor_still_503(self):
        with pytest.raises(HTTPException) as e:
            uo._r13_write(None, "单元：任意写", expected=1,
                          status=404, message="不该出现的 404")
        assert e.value.status_code == 503
        assert "服务暂时不可用" in e.value.detail

    def test_zero_rows_uses_caller_status(self):
        with pytest.raises(HTTPException) as e:
            uo._r13_write(_FakeCur(0), "单元：状态回写", expected=1,
                          status=404, message="上传记录不存在（可能已被删除），本次保存已中止")
        assert e.value.status_code == 404

    def test_exact_match_passes_and_returns_count(self):
        assert uo._r13_write(_FakeCur(3), "单元：批量写", expected=3) == 3

    def test_negative_rowcount_normalized_to_zero_aborts(self):
        """safe_rowcount 把 -1 归一为 0 → 视为未生效中止（宁可误中止，
        不放过「写没落到行却提交半套」）。"""
        with pytest.raises(HTTPException) as e:
            uo._r13_write(_FakeCur(-1), "单元：负行数", expected=2,
                          status=409, message="并发修改")
        assert e.value.status_code == 409


class TestSaveAsOutlineZeroRowWrites:
    async def test_link_update_zero_rows_404_and_full_rollback(self, db_conn):
        """归属链回写命中 0 行（记录被并发删除）→ 404，且整事务回滚：
        已重建的章节、已改的方案状态都不得提交（半套重建不可接受）。"""
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('old',?,?,'旧章','正文',1,0)", (sid, pid))
        await db_conn.commit()
        proxy = _RowsDb(
            db_conn,
            lambda s: s.startswith("UPDATE uploaded_outlines SET scheme_id"), 0)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "全新章节", "children": []}]},
                db=proxy)
        assert e.value.status_code == 404
        assert "上传记录不存在" in e.value.detail
        assert proxy.hits >= 1, "变异守卫：必须真的命中零行分支"
        await db_conn.rollback()
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,)) == 1
        cur = await db_conn.execute(
            "SELECT status FROM uploaded_outlines WHERE id=?", (rid,))
        assert (await cur.fetchone())["status"] == "parsed"

    async def test_scheme_update_zero_rows_404(self, db_conn):
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        await db_conn.commit()
        proxy = _RowsDb(
            db_conn, lambda s: s.startswith("UPDATE schemes SET outline_source"), 0)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "第一章 概况", "children": []}]},
                db=proxy)
        assert e.value.status_code == 404
        assert "方案不存在" in e.value.detail
        await db_conn.rollback()
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,)) == 0

    async def test_sections_update_shortfall_409(self, db_conn):
        """executemany UPDATE 少落一行（匹配章节被并发删除）→ 409 中止；
        preserved_content 虚报 + 父挂子断链的半套结构绝不提交。"""
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('a1',?,?,'工程概况','保留正文',1,0)",
            (sid, pid))
        await db_conn.commit()
        proxy = _RowsDb(
            db_conn, lambda s: s.startswith("UPDATE sections SET title"),
            lambda seq: max(0, len(seq) - 1))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "工程概况", "children": []}]},
                db=proxy)
        assert e.value.status_code == 409
        assert proxy.hits >= 1
        await db_conn.rollback()
        cur = await db_conn.execute(
            "SELECT outline_source, status FROM schemes WHERE id=?", (sid,))
        row = await cur.fetchone()
        assert (row["outline_source"] or "") != "上传识别"

    async def test_sections_insert_shortfall_503(self, db_conn):
        """executemany INSERT 少落行 = 写入未完整生效 → 503（可重试），
        不得提交「响应 count=全量、库里缺几节」的目录。"""
        rid = await _seed_upload(db_conn)
        _pid, sid = await _seed_scheme(db_conn)
        proxy = _RowsDb(
            db_conn,
            lambda s: s.lstrip().upper().startswith("INSERT INTO SECTIONS"),
            lambda seq: max(0, len(seq) - 1))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid, "outline": [
                    {"title": "第一章 概况", "children": []},
                    {"title": "第二章 部署", "children": []}]},
                db=proxy)
        assert e.value.status_code == 503
        assert "未完整生效" in e.value.detail
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,)) == 0

    async def test_sections_delete_shortfall_409(self, db_conn):
        """DELETE 命中数 < 预取快照数 = 章节已被并发删除，预取过期 →
        标题匹配不再可信，409 中止而非继续重建。"""
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('old',?,?,'旧章','正文',1,0)", (sid, pid))
        await db_conn.commit()
        proxy = _RowsDb(
            db_conn, lambda s: s.startswith("DELETE FROM sections"),
            lambda params: max(0, len(params) - 1))
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                rid, {"scheme_id": sid,
                      "outline": [{"title": "全新章节", "children": []}]},
                db=proxy)
        assert e.value.status_code == 409
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(
            db_conn, "SELECT COUNT(*) FROM sections WHERE id='old'") == 1

    async def test_chart_predictions_delete_exempt_from_rowcount(
            self, db_conn):
        """chart_predictions 级联 DELETE 命中 0 行是**常态**（多数章节无预测行）：
        该站点必须只判 None、不校验行数 —— 若有人给它加 expected>0 的行数校验，
        本用例会以 409/503 定向失败（保存链路被正常情况误拦）。"""
        rid = await _seed_upload(db_conn)
        pid, sid = await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('old',?,?,'旧章','正文',1,0)", (sid, pid))
        await db_conn.commit()
        proxy = _RowsDb(
            db_conn, lambda s: s.startswith("DELETE FROM chart_predictions"), 0)
        res = await uo.save_as_outline(
            rid, {"scheme_id": sid,
                  "outline": [{"title": "全新章节", "children": []}]},
            db=proxy)
        assert res["ok"] is True
        assert proxy.hits >= 1, "变异守卫：必须真的走了零行假游标分支"


class TestSaveAsLibraryZeroRowWrites:
    _BODY = {"name": "上传识别目录",
             "outline": [{"title": "第一章 概况", "children": []}]}

    async def test_insert_zero_rows_503_and_no_row(self, db_conn):
        rid = await _seed_upload(db_conn)
        proxy = _RowsDb(
            db_conn, lambda s: s.startswith("INSERT INTO outline_library"), 0)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_library(rid, dict(self._BODY), db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(db_conn, "SELECT COUNT(*) FROM outline_library") == 0

    async def test_status_update_zero_rows_404_rolls_back_library_insert(
            self, db_conn):
        """status 回写命中 0 行（记录被并发删除）→ 404，且必须连目录库 INSERT
        一起回滚 —— 否则产生与任何上传记录脱钩的孤儿「上传识别」条目。"""
        rid = await _seed_upload(db_conn)
        proxy = _RowsDb(
            db_conn, lambda s: s.startswith(
                "UPDATE uploaded_outlines SET status='library_saved'"), 0)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_library(rid, dict(self._BODY), db=proxy)
        assert e.value.status_code == 404
        assert "上传记录不存在" in e.value.detail
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(db_conn, "SELECT COUNT(*) FROM outline_library") == 0, \
            "零行状态写不得留下无主目录库条目"


class TestParseOutlineZeroRowWrite:
    async def test_record_insert_zero_rows_503_and_no_row(self, db_conn):
        """INSERT 游标非 None 但 rowcount=0：记录同样没落库，旧实现只判 None
        会漏放 → 响应回 {"id": rid} 假成功，后续保存必 404。"""
        pid, _sid = await _seed_scheme(db_conn)
        proxy = _RowsDb(
            db_conn,
            lambda s: s.lstrip().upper().startswith("INSERT INTO UPLOADED_OUTLINES"),
            0)
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("大纲.txt", _OUTLINE_TXT),
                                   scheme_name="", reorganize=False,
                                   project_id=pid, db=proxy)
        assert e.value.status_code == 503
        assert proxy.hits >= 1
        await db_conn.rollback()
        assert await _count(db_conn,
                            "SELECT COUNT(*) FROM uploaded_outlines") == 0
