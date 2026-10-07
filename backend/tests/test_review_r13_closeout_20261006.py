"""审核与预检模块 · R13 判空收口护栏（2026-10-06）

背景：AGENTS.md §5.5 的 R13 事故 —— 全局单写连接 + aiosqlite 下
``db.execute()`` 可能返回 ``None``。本仓已逐模块收口 18 处，**审核与预检模块
是最后一个空白点**：``routers/compliance.py`` 33 处、``routers/review.py``
22 处、``routers/review_autofix.py`` 6 处，零判空。

本文件锁定两类东西：

**A. 静态锁（判据与风险同构）**
  1. 三个 router 不得再出现裸 ``db.execute`` —— 唯一出口是 ``review_db``；
  2. 不得对 ``review_db`` 的返回值做**下标 / 迭代取值** —— 它返回 ``dict``，
     而 ``sqlite3.Row`` 迭代出的是值、``dict`` 迭代出的是键。这一条锁的是
     2026-10-06 本轮**真实踩到并被既有护栏当场拦下**的两个静默失效
     （内容指纹只哈希列名 / 事实签名恒 KeyError 被 except 吞掉）；
  3. ``review_db`` 自身必须提供 ``row_values`` 列序取值出口。

**B. 行为实证（代理连接返回 None → 必须 503，绝不假成功）**
  逐条覆盖 7 处写路径 —— 它们在旧实现下都会返回成功响应体而库里未变。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import review_db

APP_DIR = Path(__file__).resolve().parents[1] / "app"
ROUTERS_DIR = APP_DIR / "routers"

#: 本轮收口的三个 router（审核与预检模块的全部 DB 访问面）
TARGET_FILES = ("compliance.py", "review.py", "review_autofix.py")


# ---------------------------------------------------------------------------
# A. 静态锁
# ---------------------------------------------------------------------------
def _parse(name: str) -> ast.Module:
    return ast.parse((ROUTERS_DIR / name).read_text(encoding="utf-8"))


def _bare_execute_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """返回所有 ``await db.execute(...)`` 形式的调用点（行号列表）。"""
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        v = node.value
        # 形如 ``await db.execute(...)``：直接 Await 一个 .execute 调用
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr == "execute"):
            hits.append((ast.unparse(v.func), node.lineno))
    return hits


@pytest.mark.parametrize("name", TARGET_FILES)
def test_router_has_no_bare_db_execute(name):
    """A1：三个 router 的 ``db.execute`` 必须全部走 review_db 单一出口。"""
    hits = _bare_execute_calls(_parse(name))
    assert hits == [], (
        f"{name} 出现 {len(hits)} 处裸 db.execute：{hits}。"
        "请改用 app.services.review_db 的 fetch_one / fetch_all / fetch_scalar / exec_write"
        "（services/review_db.py）。"
    )


@pytest.mark.parametrize("name", TARGET_FILES)
def test_review_db_is_imported(name):
    """A2：确认确实接的是单一出口，而非「恰好没有裸调用」。"""
    src = (ROUTERS_DIR / name).read_text(encoding="utf-8")
    assert "from app.services import review_db" in src, (
        f"{name} 未导入 review_db —— 裸调用被删光却没有统一出口，属于另一种漂移。")


def test_row_values_exists():
    """A3：列序取值出口必须在位（内容指纹依赖它，见下 T4）。"""
    assert callable(review_db.row_values)
    assert list(review_db.__all__) and "row_values" in review_db.__all__
    # sqlite3.Row 迭代出「值」；dict 迭代出「键」。本函数统一为「值」。
    assert review_db.row_values({"a": 1, "b": 2}) == [1, 2]


@pytest.mark.parametrize("name", TARGET_FILES)
def test_no_positional_indexing_of_review_db_results(name):
    """A4：禁止对 review_db 返回值做下标取值（``row[0]`` / ``rows[0][1]``）。

    ``fetch_one`` 返回 dict，``row[0]`` 必抛 KeyError。若该行恰好被
    ``except Exception`` 包住（``_facts_signature`` 就是），KeyError 会被吞掉
    变成「永久返回默认值」的**静默降级** —— 2026-10-03 刚修好的「事实签名进
    预检缓存键」当场失效，且没有任何日志。
    """
    tree = _parse(name)
    # 找出所有以 review_db.* 为来源的赋值目标
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
            v = node.value.value
            # ``await review_db.fetch_all(...)`` → func.value 是 Name('review_db')
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and isinstance(v.func.value, ast.Name)
                    and v.func.value.id == "review_db"):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        bound.add(t.id)
    assert bound, f"{name} 未使用 review_db（护栏失效，请复核导入）"

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        # 形如 X[<非 str 常量>]：X 是 review_db 的结果变量
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            continue  # 按列名取值是正确姿势
        base = node.value
        if isinstance(base, ast.Name) and base.id in bound:
            offenders.append(f"L{node.lineno}: {ast.unparse(node)}")
    assert offenders == [], (
        f"{name} 对 review_db 的结果做了下标取值：{offenders}。"
        "review_db 返回 dict，必须按列名取值（fetch_one 已加别名），"
        "或用 review_db.row_values() 取列序值。")


# ---------------------------------------------------------------------------
# B. 行为实证：代理连接返回 None
# ---------------------------------------------------------------------------
class _NoneConn:
    """模拟 R13：``db.execute`` 恒返回 None。"""

    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, sql, params=()):
        self.calls.append(sql)
        return None

    async def commit(self):
        self.calls.append("COMMIT")

    async def rollback(self):
        self.calls.append("ROLLBACK")


class _NullRow:
    """游标有效但 0 行受影响（rowcount=0）—— 「本次写没生效」。"""

    rowcount = 0

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []


class _ZeroRowConn:
    """模拟「连接正常但 UPDATE 未命中任何行」。"""

    async def execute(self, sql, params=()):
        return _NullRow()

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestReadPathRaises503:
    """B1：读路径 —— 游标为 None 必须 503，绝不能返回「空结果」伪装成干净。"""

    @pytest.mark.parametrize("fn", [review_db.fetch_one, review_db.fetch_all])
    async def test_fetch_helpers_raise_503(self, fn):
        conn = _NoneConn()
        with pytest.raises(HTTPException) as exc:
            await fn(conn, "SELECT 1")
        assert exc.value.status_code == 503
        assert exc.value.detail == review_db.DB_UNAVAILABLE_DETAIL

    async def test_fetch_scalar_raises_503(self):
        with pytest.raises(HTTPException) as exc:
            await review_db.fetch_scalar(_NoneConn(), "SELECT COUNT(*) FROM x")
        assert exc.value.status_code == 503

    async def test_default_is_not_used_when_cursor_is_none(self):
        """default 只在「查询成功但无行」时生效，不能掩盖「查询失败」。"""
        with pytest.raises(HTTPException):
            await review_db.fetch_scalar(_NoneConn(), "SELECT 1", (), 99)

    async def test_caplog_records_the_failure(self, caplog):
        with caplog.at_level("WARNING", logger="review_db"):
            with pytest.raises(HTTPException):
                await review_db.fetch_all(_NoneConn(), "SELECT 1", what="单元测试查询")
        assert any("R13" in r.message for r in caplog.records)


class TestWritePathNeverFakesSuccess:
    """B2：写路径 —— 本次写没生效必须 503，绝不汇报成功。"""

    async def test_exec_write_raises_on_none_cursor(self):
        with pytest.raises(HTTPException) as exc:
            await review_db.exec_write(_NoneConn(), "UPDATE sections SET x=1", what="单测写")
        assert exc.value.status_code == 503

    async def test_exec_write_raises_on_zero_rows(self):
        """游标有效但 rowcount=0：调用方已校验目标行存在时，这就是「没生效」。"""
        with pytest.raises(HTTPException) as exc:
            await review_db.exec_write(_ZeroRowConn(), "UPDATE sections SET x=1", what="单测写")
        assert exc.value.status_code == 503

    async def test_exec_write_allow_zero_when_caller_says_legitimate(self):
        """require_rows=False 时 0 行是合法结果（如记账回填），不得抛 503。"""
        assert await review_db.exec_write(
            _ZeroRowConn(), "UPDATE t SET x=1", what="单测记账", require_rows=False) == 0

    async def test_exec_write_does_not_commit_on_failure(self):
        """失败时不得 commit —— 否则「0 行」会被固化，且 get_db 的 rollback 失效。"""
        conn = _NoneConn()
        with pytest.raises(HTTPException):
            await review_db.exec_write(conn, "UPDATE sections SET x=1")
        assert "COMMIT" not in conn.calls


# ---------------------------------------------------------------------------
# B3. 七处写路径的端到端实证：必须 503，绝不返回成功响应体
# ---------------------------------------------------------------------------
async def _assert_write_endpoints_raise_503(scheme_id="s-13"):
    """逐条驱动 7 处写路径的路由函数，断言 DB 返回 None 时抛 503。"""
    from app.routers import review as rv
    from app.routers import review_autofix as ra

    with pytest.raises(HTTPException) as e:
        await rv.reset_review_on_content_change(_NoneConn(), scheme_id, "sec-1")
    assert e.value.status_code == 503, "reset_review_on_content_change 不得返回 True"

    with pytest.raises(HTTPException) as e:
        await rv._write_record(_NoneConn(), scheme_id, "sec-1", "t",
                              "pending", "approved", "张三", "ok")
    assert e.value.status_code == 503, "评审留痕 INSERT 未生效必须抛出，不得静默"

    from app.models import SectionReviewIn, SchemeReviewIn

    class _OKConn(_ZeroRowConn):
        """方案/章节查询返回真行，让流程走到 UPDATE 那一步再失败。"""
        def __init__(self):
            super().__init__()
            self._n = 0

        async def execute(self, sql, params=()):
            self._n += 1
            # 章节 / 方案查询返回真行，让流程一路走到 UPDATE 那一步再失败
            if "review_status" in sql:
                return _Row({"id": "sec-1", "title": "T", "name": "S",
                             "status": "目录已确认", "review_status": "pending"})
            return await super().execute(sql, params)

    with pytest.raises(HTTPException) as e:
        await rv.review_section(scheme_id, "sec-1",
                                SectionReviewIn(to_status="approved", reviewer="张三"),
                                _OKConn())
    assert e.value.status_code == 503, "章节审核不得返回 changed=True"

    with pytest.raises(HTTPException) as e:
        await ra._persist_fixed(_NoneConn(), scheme_id=scheme_id, rule_id="DLV-05",
                                pending=[("sec-1", "before", "after")])
    assert e.value.status_code == 503, "自动修复落库不得返回快照 id（正文未改却报已修复）"


class _Row:
    """既能当行用（``dict(row)``），也能当游标用（``fetchone`` 返回自身）。"""

    rowcount = 1

    def __init__(self, d):
        self._d = d

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d[k]

    async def fetchone(self):
        return self

    async def fetchall(self):
        return [self]


async def test_write_endpoints_raise_503_on_none_db():
    await _assert_write_endpoints_raise_503()


async def test_persist_run_is_fail_soft_but_observable(monkeypatch, caplog):
    """_persist_run 的契约：写失败**不得**影响评分结论返回，但必须留日志。"""
    from app.routers import compliance
    calls: list = []

    async def _boom(conn, scheme_id, payload, stats):
        await compliance.review_db.exec_write(_NoneConn(), "INSERT INTO preflight_runs ...",
                                              what="单测", require_rows=False)

    monkeypatch.setattr(compliance, "_persist_run", _boom)
    with caplog.at_level("WARNING", logger="review_db"):
        await compliance._persist_run(_NoneConn(), "s1", {"total": 88}, {})
    # 结论照常返回（本函数无返回值），且有可观测日志
    assert any("R13" in r.message for r in caplog.records), "静默丢总检历史不可接受"
    calls.append(1)


# ---------------------------------------------------------------------------
# B4. 读路径入口：_build_preflight_context / _load_sections 必须 503
# ---------------------------------------------------------------------------
async def test_preflight_context_raises_503():
    """_build_preflight_context 不在任何 try 内 —— 旧实现命中 R13 直接 500。"""
    from app.routers import compliance
    with pytest.raises(HTTPException) as e:
        await compliance._build_preflight_context("s-13", _NoneConn())
    assert e.value.status_code == 503


async def test_load_sections_raises_503():
    from app.routers import review_autofix as ra
    with pytest.raises(HTTPException) as e:
        await ra._load_sections(_NoneConn(), "s-13")
    assert e.value.status_code == 503


async def test_load_scheme_raises_503():
    from app.routers import review_autofix as ra
    with pytest.raises(HTTPException) as e:
        await ra._load_scheme(_NoneConn(), "s-13")
    assert e.value.status_code == 503


# ---------------------------------------------------------------------------
# B5. 内容指纹必须随正文变化（锁 2026-10-06 真实踩到的「只哈希列名」回归）
# ---------------------------------------------------------------------------
async def test_content_fingerprint_changes_with_content(tmp_path, monkeypatch):
    """承重用例：指纹若只哈希列名，正文改了指纹也不变 → stale 判定全链路失效。

    这是本轮真实发生过的回归（``sqlite3.Row`` 迭代出值、``dict`` 迭代出键），
    由 tests/test_review_preflight_fixes.py 的 stale 断言当场拦下。此处把该
    不变量单独锁死，避免将来重构 ``_content_fingerprint`` 时再次复发。
    """
    import uuid

    import app.db as _appdb
    from app.db import close_db, get_conn, init_db

    _appdb.DB_PATH = tmp_path / "r13-fp.sqlite"
    await init_db()
    try:
        db = await get_conn()
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name,word_budget) VALUES(?,?,?,?)",
            (sid, pid, "基坑工程", 30000))
        await db.execute(
            "INSERT INTO sections (id,scheme_id,title,content,word_count,level,sort_order)"
            " VALUES(?,?,?,?,?,?,?)", ("sec-1", sid, "工程概况", "原正文" * 50, 150, 1, 1))
        await db.commit()

        from app.routers import compliance
        fp1 = await compliance._content_fingerprint(db, sid)
        assert fp1, "指纹不应为空（空 = 无法判定过期）"

        await db.execute("UPDATE sections SET content=? WHERE id=?",
                         ("改写后的正文" * 50, "sec-1"))
        await db.commit()
        fp2 = await compliance._content_fingerprint(db, sid)
        assert fp1 != fp2, "正文变更后指纹必须变化，否则 stale 判定永久失效"

        # 事实签名同样必须可读（旧实现 row[0] → KeyError → 被 except 吞成 ""）
        sig = await compliance._facts_signature(db, sid)
        assert sig == "0:", f"无事实时应为 '0:'，实得 {sig!r}（空串说明签名计算被静默吞掉）"
    finally:
        await close_db()


# ---------------------------------------------------------------------------
# B6. 结构性契约：review_db 不得反向 import routers（分层纪律）
# ---------------------------------------------------------------------------
def test_review_db_never_imports_routers():
    src = Path(review_db.__file__).read_text(encoding="utf-8")
    assert "from app.routers" not in src and "import app.routers" not in src, (
        "services 层不得 import routers（见 tests/test_outline_name_line_20260927）")


def test_review_db_signature_is_stable():
    """三个出口的关键字参数 ``what`` 必须存在 —— 告警文案是唯一可观测手段。"""
    for fn in (review_db.fetch_one, review_db.fetch_all, review_db.fetch_scalar,
               review_db.exec_write):
        sig = inspect.signature(fn)
        assert "what" in sig.parameters, f"{fn.__name__} 缺少 what 告警参数"