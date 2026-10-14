"""T1（2026-10-03）· 导出缓存保留策略共享 helper 回归测试。

根因：export_pdf 分支此前只 INSERT INTO export_cache 从不裁剪 —— DOCX 分支
有完整的 prune（删陈旧行 + 连带删磁盘文件），PDF 分支完全没有 → DB 行与
EXPORTS_DIR 磁盘文件随 PDF 导出次数无限增长。

修法：把 DOCX 内联裁剪逻辑逐字提取为 `_prune_export_cache` 共享 helper，
DOCX（行为零变化）与 PDF（补齐同一策略）共同调用，跨格式合计最多 5 份/方案。
"""
import ast
import uuid
from pathlib import Path

import app.db as _appdb
import app.routers.export as export_mod
import pytest
from app.db import close_db, get_conn, init_db

EXPORT_PY = Path(export_mod.__file__)
KEEP = 5  # 保留策略：最多 5 份/方案（与前端 tooltip 说明对齐）


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "export_prune.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, "s"))
    try:
        yield db, sid, tmp_path
    finally:
        await close_db()


async def _insert_cache(db, sid, path, created_at):
    """显式 created_at 插入（裁剪按 created_at DESC, rowid DESC 排序）。"""
    await db.execute(
        "INSERT INTO export_cache "
        "(id, project_id, scheme_id, config_hash, content_fingerprint, cache_key, "
        " result_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "", sid, "cfg", f"fp-{uuid.uuid4().hex[:8]}",
         f"{sid}_cfg", str(path), created_at))


async def _row_count(db, sid):
    cur = await db.execute("SELECT COUNT(*) AS n FROM export_cache WHERE scheme_id=?",
                           (sid,))
    return dict(await cur.fetchone())["n"]


async def test_prune_keeps_five_newest_and_deletes_stale_files(ctx):
    """超过 5 份时：保留最新 5 行，更旧行删除且磁盘文件连带清理。"""
    db, sid, tmp_path = ctx
    files = []
    for i in range(7):
        f = tmp_path / f"old_{i}.docx"
        f.write_bytes(b"x")
        files.append(f)
        await _insert_cache(db, sid, f, f"2026-10-01 10:00:{i:02d}")
    await db.commit()

    protect = tmp_path / "current.docx"
    protect.write_bytes(b"new")
    await export_mod._prune_export_cache(db, sid, protect_path=protect)

    assert await _row_count(db, sid) == KEEP
    # 最早的 2 份（i=0,1）行与文件都应被清理
    assert not files[0].exists()
    assert not files[1].exists()
    # 保留的 5 份与当前产物文件完好
    for f in files[2:]:
        assert f.exists()
    assert protect.exists()


async def test_prune_zombie_rows_pruned_even_within_top5(ctx):
    """文件已丢失的僵尸行即使排在最新 5 位内也照裁（口径与旧 DOCX 逻辑一致）。"""
    db, sid, tmp_path = ctx
    real = []
    for i in range(3):
        f = tmp_path / f"real_{i}.docx"
        f.write_bytes(b"x")
        real.append(f)
        await _insert_cache(db, sid, f, f"2026-10-01 10:0{i}:00")
    ghosts = [tmp_path / f"ghost_{i}.docx" for i in range(3)]
    for i, g in enumerate(ghosts):
        await _insert_cache(db, sid, g, f"2026-10-01 09:0{i}:00")  # 不落盘
    await db.commit()

    await export_mod._prune_export_cache(db, sid, protect_path=tmp_path / "cur.docx")

    assert await _row_count(db, sid) == 3
    for f in real:
        assert f.exists()


async def test_prune_protected_file_not_deleted_even_if_row_stale(ctx):
    """当前产物文件受 keep_paths 保护：即便其行被判 stale，文件也不得被删。"""
    db, sid, tmp_path = ctx
    for i in range(7):
        f = tmp_path / f"old_{i}.docx"
        f.write_bytes(b"x")
        await _insert_cache(db, sid, f, f"2026-10-01 10:00:{i:02d}")
    protect = tmp_path / "current.pdf"
    protect.write_bytes(b"new")
    # 把当前产物登记为一行并置旧 created_at，模拟「同秒插入次序异常」场景：
    # 行会被判 stale，但 keep_paths 必须保住文件（误删 → FileResponse 404/500）。
    await _insert_cache(db, sid, protect, "2026-10-01 08:00:00")
    await db.commit()

    await export_mod._prune_export_cache(db, sid, protect_path=protect)

    assert protect.exists()


async def test_prune_fail_soft_on_none_cursor(ctx, monkeypatch):
    """R13 守卫：db.execute 返回 None（连接/事务异常）时 fail-soft 跳过，不抛错。"""
    db, sid, tmp_path = ctx
    f = tmp_path / "a.docx"
    f.write_bytes(b"x")
    await _insert_cache(db, sid, f, "2026-10-01 10:00:00")
    await db.commit()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(db, "execute", _none)
    await export_mod._prune_export_cache(db, sid, protect_path=tmp_path / "cur.docx")
    # 不抛异常即通过；文件未被误删
    assert f.exists()


def _route_fn(name):
    tree = ast.parse(EXPORT_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in export.py")


@pytest.mark.parametrize("route", ["export_docx", "export_pdf"])
def test_both_branches_call_shared_prune(route):
    """静态 parity 锁：DOCX 与 PDF 都必须经统一写存 helper 接入共享裁剪，
    防止未来新格式分支再次漏接保留策略（重演 T1）。

    ✅ 2026-10-14 收口后：裁剪下沉到 _persist_export_cache_row，路由只调 helper。
    """
    route_calls = [n.func.id for n in ast.walk(_route_fn(route))
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_persist_export_cache_row" in route_calls, (
        f"{route} 未接入统一写存 helper _persist_export_cache_row")
    # helper 内部必须含裁剪（防未来有人把 prune 从写存里摘掉）
    helper_calls = [n.func.id for n in ast.walk(_route_fn("_persist_export_cache_row"))
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_prune_export_cache" in helper_calls, "写存 helper 未接入共享裁剪"


def test_helper_no_inline_duplicate_in_routes():
    """防分叉：裁剪判定逻辑必须只存在于共享 helper，路由分支内不得重抄
    （扫标志性的「keep_paths」局部变量，路由函数体内出现即说明有人复制了一份）。"""
    for route in ("export_docx", "export_pdf"):
        names = {n.id for n in ast.walk(_route_fn(route)) if isinstance(n, ast.Name)}
        assert "keep_paths" not in names, f"{route} 内联复制了裁剪逻辑"


# ---------------------------------------------------------------------------
# 2026-10-13（P1）· export_cache 写入的提交语义
#
# 根因：DOCX 分支的「INSERT INTO export_cache」与两分支 `_prune_export_cache`
# 的 DELETE 此前都**没有任何显式 commit**，只靠后面 `_record_export_review_trace`
# 的 commit 副作用顺带落盘。而该 helper 的既定语义是「留痕失败只记日志并
# rollback，绝不阻断导出」—— 一旦它内部异常走 except 分支，刚写的缓存行与刚删的
# 陈旧行会被**一并回滚**：用户本次导出成功，但缓存永不落盘（下一次同内容导出又
# 付一次全量渲染成本）、「最多 5 份」保留策略也不生效（行数缓慢无限增长，而
# /cache-status 仍显示有缓存，无从察觉）。
#
# 判据锚定「提交顺序」这一契约本身，而非「某处是否出现某字符串」：
# 只要每个「写缓存语句 → 留痕调用」之间仍有显式 commit，测试即通过；
# 若未来又把 commit 挪到留痕之后（或删掉），定向失败。
# ---------------------------------------------------------------------------

_TRACE = "_record_export_review_trace"


def _stmt_line_of_insert(top):
    """返回 `top` 内**容纳写入的最近语句**的行号。

    2026-10-13 锚点订正（P2 收口引入 wrapper-only try/finally 后）：
    旧实现取 `top` 本身的行号 —— 当该写入被 `try:` / `if cacheable:` 等
    复合语句包住时，顶层语句行号落在**包裹者**上（`try:` 行），既可能早于
    缓存命中分支的留痕调用，导致「第一个后续留痕」选错、commit 判定失真。
    现改为沿引用链向下找**最近的确切写语句**（`_db_exec`/`db.execute` 调用），
    语义与旧锚点一致但更精确 —— 不会因包裹层而漂移。
    """
    focus = None
    for c in ast.walk(top):
        if (isinstance(c, ast.Constant) and isinstance(c.value, str)
                and "INSERT" in c.value and "export_cache" in c.value):
            focus = c
            break
    if focus is None:
        return None

    best = None

    def dfs(node, path_stmt):
        nonlocal best
        if isinstance(node, (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign)):
            path_stmt = path_stmt + [node]
        if node is focus:
            best = path_stmt[-1] if path_stmt else top
            return
        for child in ast.iter_child_nodes(node):
            if best is not None:
                return
            dfs(child, path_stmt)

    dfs(top, [])
    return (best.lineno if best is not None else top.lineno)


def _write_points_and_trace(fn):
    """提取路由内「写缓存语句的源码行号」与「留痕调用行号」。

    写点判定：字符串常量片段同时含 INSERT 与 export_cache（覆盖 _db_exec 封装
    与直接 db.execute 两种写法）；只扫函数**自身**的 Body 行，避免踩到内嵌
    闭包或同名 helper 的语句。写点行号取**确切写语句**（见 `_stmt_line_of_insert`），
    包裹层（try/if）不参与定位。
    """
    writes, trace = [], []
    for st in fn.body:
        seg = " ".join(
            str(c.value) for c in ast.walk(st)
            if isinstance(c, ast.Constant) and isinstance(c.value, str))
        if "INSERT" in seg and "export_cache" in seg:
            writes.append(_stmt_line_of_insert(st) or st.lineno)
        for n in ast.walk(st):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == _TRACE):
                trace.append(n.lineno)
    return writes, trace


@pytest.mark.parametrize("route", ["export_docx", "export_pdf"])
def test_cache_writes_committed_before_review_trace(route):
    """每个写缓存语句与**其后的**留痕调用之间必须存在显式 commit。

    ⚠️ 锚点必须是「写入之后的第一次留痕调用」：缓存命中分支的留痕调用合法地
    出现在写入之前（命中时本就不写缓存），若把它当锚点本测试会恒失败。

    ✅ 2026-10-14 收口后：写入与提交均在 _persist_export_cache_row 内，
    留痕在 _finalize_export_delivery 内。契约改为两段：
      ① 写存 helper 内每个 INSERT 之后必须有 commit（双提交第一道）；
      ② 路由函数内写存 helper 调用必须在留痕收尾 helper 调用之前
        （写存落盘后才能留痕 + 回收 + 注销）。
    """
    persist = _route_fn("_persist_export_cache_row")
    writes, _ = _write_points_and_trace(persist)
    assert writes, "写存 helper 未找到 export_cache 写入语句（测试自身失效）"
    commits = [n.lineno for n in ast.walk(persist)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "db" and n.func.attr == "commit"]
    for w in writes:
        after = [c for c in commits if c > w]
        assert after, (
            f"写存 helper: 写入(L{w}) 之后没有显式 await db.commit() → "
            "写入只靠后续副作用落盘；异常回滚会丢缓存行，缓存永不落盘")
    # 路由内：写存必须先于留痕收尾
    fn = _route_fn(route)
    persist_calls = [n.lineno for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "_persist_export_cache_row"]
    finalize_calls = [n.lineno for n in ast.walk(fn)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id == "_finalize_export_delivery"]
    assert persist_calls and finalize_calls, (
        f"{route}: 缺写存/留痕收尾 helper 调用（测试自身失效）")
    # 每个写存调用之后必须有留痕收尾调用（degraded 路径无写存、只有收尾，
    # 合法地早于任何 persist —— 故只校验 persist 之后的 finalize）
    for pc in persist_calls:
        after_fin = [c for c in finalize_calls if c > pc]
        assert after_fin, (
            f"{route}: 写存(L{pc}) 之后没有留痕收尾调用 → "
            "写存落盘前就留痕，取消/断连发生在写存期会把未落盘行当成功留痕")


def test_pdf_prune_delete_also_committed():
    """写存 helper 内 prune 之后必须有 commit（双提交第二道，PDF 2026-10-13 口径）。

    ✅ 2026-10-14 收口后：prune 与其后的 commit 均在 _persist_export_cache_row 内。
    """
    persist = _route_fn("_persist_export_cache_row")
    prune = [n.lineno for n in ast.walk(persist)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_prune_export_cache"]
    assert prune, "写存 helper 未接入共享裁剪（测试自身失效）"
    commits = [n.lineno for n in ast.walk(persist)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "db" and n.func.attr == "commit"]
    after_prune = [c for c in commits if c > prune[0]]
    assert after_prune, (
        "写存 helper: prune(L%d) 之后没有 commit → 裁剪 DELETE 属未提交写入，"
        "异常回滚时「最多 5 份」保留策略被静默跳过，export_cache 行数与磁盘文件"
        "会随导出次数无限增长" % prune[0])
    # export_pdf 必须经此 helper（与 DOCX 同口径）
    pdf_calls = [n.func.id for n in ast.walk(_route_fn("export_pdf"))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_persist_export_cache_row" in pdf_calls, "export_pdf 未接入统一写存 helper"

