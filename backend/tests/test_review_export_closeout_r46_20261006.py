"""审核与预检 · 导出模块 R46 收口护栏（2026-10-06）

三个真实缺陷的回归锁：

1. **review.py::submit_scheme_review** 读「最近一次总检」时 ``ORDER BY`` 只写
   ``created_at``（TEXT 秒级精度）。同一秒内两次总检的返回次序属 SQL 语义未
   定义，取到哪一行取决于查询计划。取到旧行有两类用户可见错误：
     ① 读到旧 blocked 行 → **假阻断**（后一条已放行却报 422「存在阻断项」）；
     ② 读到旧 released 行 → **绕过门禁**（后一条已阻断却放行）。
   补 ``rowid DESC`` 次序兜底（rowid 单调递增，等价于真实写入顺序）。
2. **review.py::review_records / review_summary** 与 **export.py::cache_status**
   —— 同类的「秒级时间戳排序无次序兜底」：分页窗口取成任意行（同批记录在不同
   页重复出现或整条漏掉），「最近 5 份缓存」取成任意 5 份（刚导出的那份反而被
   挤掉）。同类判据在此前已散落在 6+ 处，本轮把三处收敛到同一口径。
3. **sections.py::reset_content** —— sections.py 里**唯一漏接导出缓存失效**的
   正文写路径（create/update/delete/save-outline/reorder 五处均已接）。清空正文
   后旧 export_cache 行从此再无命中可能，却既不失效也不裁剪 → 成为孤儿行，
   /cache-status 仍报「有效缓存」，磁盘 .docx/.pdf 残留。

护栏形态：行为锁（断言正确次序）+ 静态锁（禁止 ORDER BY 只写 created_at 的写法
回退）。静态锁才是能在 A/B 反向验证中定向失败的那一层 —— 见文件末尾注释。
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.db as _appdb
from app.db import close_db, get_conn, init_db
from app.models import SchemeReviewIn
from app.routers.export import cache_status
from app.routers.review import _ALLOWED_TRANSITIONS, review_records, review_summary, submit_scheme_review
from app.routers.sections import reset_content

APP_DIR = Path(__file__).resolve().parents[1] / "app"
ROUTERS_DIR = APP_DIR / "routers"
#: 前端状态流转字面量（护栏直接解析它，禁止手写镜像 —— 镜像护栏此前恒通过）
REVIEW_PANEL_TSX = (APP_DIR.parent.parent / "frontend"
                    / "src" / "components" / "review" / "ReviewWorkflowPanel.tsx")

#: 同秒时间戳（created_at 是 TEXT 秒级精度，这是复现缺陷的唯一前提）
_TS = "2026-10-06 12:00:00"


@pytest.fixture
async def db_ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "r46-review-export.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,0)",
        (sid, pid, "专项方案", "目录已确认"))
    await db.commit()
    yield db, pid, sid
    await close_db()


async def _insert_section(db, sid, pid, title="第一章 工程概况", content="已有正文内容",
                          updated_at=None, level=1, sort_order=0):
    """插入一个已生成正文的章节，返回 section_id。"""
    sec_id = uuid.uuid4().hex
    cols = ("id, scheme_id, project_id, parent_id, title, description, level,"
            " status, word_count, word_budget, content, review_status, sort_order")
    params = (sec_id, sid, pid, "", title, "", level, "generated",
              len(content), 0, content, "", sort_order)
    await db.execute(
        f"INSERT INTO sections ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", params)
    if updated_at:
        await db.execute("UPDATE sections SET updated_at=? WHERE id=?", (updated_at, sec_id))
    return sec_id


# ---------------------------------------------------------------------------
# 1. /submit 读「最近一次总检」必须按 rowid 兜底
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_submit_picks_newest_row_when_created_at_ties(db_ctx):
    """同秒两条总检：旧 blocked=1 在前、新 released=1 在后。

    修复前 ORDER BY 只写 created_at，两次总检同秒 → 可能取到旧行而假阻断。
    """
    db, pid, sid = db_ctx
    sec = await _insert_section(db, sid, pid, updated_at="2026-10-06 11:59:00")
    # 两条同秒总检：第一条阻断，第二条放行（真实场景：整改后重跑总检）
    for idx, (blocked, released, total) in enumerate([(1, 0, 40.0), (0, 1, 90.0)]):
        await db.execute(
            "INSERT INTO preflight_runs (id, scheme_id, project_id, content_fingerprint,"
            " total, grade, verdict, released, blocked, counts, dimensions, findings, stats,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"r{idx}", sid, pid, "", total, "D" if blocked else "A", "未通过" if blocked else "通过",
             released, blocked, "{}", "[]", "[]", "{}", _TS))
    await db.commit()
    res = await submit_scheme_review(
        sid, SchemeReviewIn(to_status="approved", reviewer="评审人"), db)
    assert res["ok"] is True, "同秒两次总检下 /submit 假阻断：取到了旧的 blocked 行"
    assert res["to_status"] == "approved"
    # 行已实际落库（防 review_db.exec_write 假成功）
    cur = await db.execute("SELECT review_status FROM schemes WHERE id=?", (sid,))
    assert (await cur.fetchone())[0] == "approved"
    _ = sec


@pytest.mark.asyncio
async def test_submit_gate_still_blocks_when_latest_row_blocked(db_ctx):
    """反向护栏：兜底取新行不得变成「永远放行」——最新一条阻断时仍须 422。"""
    db, pid, sid = db_ctx
    await _insert_section(db, sid, pid, updated_at="2026-10-06 11:59:00")
    for idx, (blocked, released, total) in enumerate([(0, 1, 90.0), (1, 0, 40.0)]):
        await db.execute(
            "INSERT INTO preflight_runs (id, scheme_id, project_id, content_fingerprint,"
            " total, grade, verdict, released, blocked, counts, dimensions, findings, stats,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"r{idx}", sid, pid, "", total, "A" if released else "D",
             "通过" if released else "未通过", released, blocked, "{}", "[]", "[]", "{}", _TS))
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await submit_scheme_review(sid, SchemeReviewIn(to_status="approved", reviewer="评审人"), db)
    assert ei.value.status_code == 422, "最新一条总检仍阻断时 /submit 不应放行"


# ---------------------------------------------------------------------------
# 2. 分页 / 预览窗口：秒级时间戳排序必须有次序兜底
# ---------------------------------------------------------------------------
async def _insert_review_records(db, sid, pid, n=12):
    """批量写入同秒评审留痕，返回按写入顺序排列的 record id。"""
    ids = []
    for i in range(n):
        rid = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO review_records (id, scheme_id, project_id, section_id, section_title,"
            " from_status, to_status, reviewer, comment, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, sid, pid, f"sec{i}", f"第{i}章", "", "approved", f"评审人{i}", f"意见{i}", _TS))
        ids.append(rid)
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_review_records_pagination_is_contiguous_when_created_at_ties(db_ctx):
    """同秒写入的评审留痕，offset 翻页必须连续：全量覆盖、无重复。"""
    db, pid, sid = db_ctx
    ids = await _insert_review_records(db, sid, pid, n=12)
    page_ids: list[str] = []
    for off in (0, 5, 10):
        res = await review_records(sid, "", 5, off, db)
        assert res["total"] == 12, "total 必须等于全部记录数"
        assert len(res["items"]) == min(5, 12 - off)
        page_ids.extend(it["id"] for it in res["items"])
    assert len(set(page_ids)) == 12, f"翻页出现重复或漏行：共取到 {len(set(page_ids))} 条"
    assert set(page_ids) == set(ids), "翻页取到的集合与写入集合不一致"


@pytest.mark.asyncio
async def test_cache_status_returns_newest_rows_when_created_at_ties(db_ctx, tmp_path):
    """同一秒导出多次时，「最近 5 份缓存」必须取最新 5 行（而非任意 5 行）。"""
    db, pid, sid = db_ctx
    paths = []
    for i in range(6):
        p = tmp_path / f"export-{i}.docx"
        p.write_bytes(b"PK\x03\x04dummy")
        await db.execute(
            "INSERT INTO export_cache (id, project_id, scheme_id, config_hash,"
            " content_fingerprint, cache_key, result_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, pid, sid, "cfg", f"fp{i}", f"k{i}", str(p), _TS))
        paths.append(str(p))
    await db.commit()
    res = await cache_status(sid, db=db)
    got = [it["result_path"] for it in res["items"]]
    assert len(got) == 5
    assert got == list(reversed(paths[-5:])), (
        f"最近 5 份缓存取错：应为最新 5 行的逆序，实际 {[Path(g).name for g in got]}")
    assert res["total"] == 5 and res["stale"] == 0


@pytest.mark.asyncio
async def test_cache_status_marks_missing_files_as_stale(db_ctx, tmp_path):
    """僵尸行（result_path 指向已删除的文件）必须计入 stale，不计入 total。"""
    db, pid, sid = db_ctx
    await db.execute(
        "INSERT INTO export_cache (id, project_id, scheme_id, config_hash,"
        " content_fingerprint, cache_key, result_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, sid, "cfg", "fp", "k",
         str(tmp_path / "已删除的导出文件.docx"), _TS))
    await db.commit()
    res = await cache_status(sid, db=db)
    assert res["total"] == 0
    assert res["stale"] == 1
    assert res["items"][0]["exists"] is False


# ---------------------------------------------------------------------------
# 3. reset_content 必须失效导出缓存（sections.py 唯一漏接的正文写路径）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reset_content_invalidates_export_cache(db_ctx, tmp_path):
    """清空正文后旧缓存行与磁盘产物一并清理（不留孤儿）。"""
    db, pid, sid = db_ctx
    await _insert_section(db, sid, pid, content="要被清掉的正文" * 20)
    artifact = tmp_path / "cached-export.docx"
    artifact.write_bytes(b"PK\x03\x04dummy")
    await db.execute(
        "INSERT INTO export_cache (id, project_id, scheme_id, config_hash,"
        " content_fingerprint, cache_key, result_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, sid, "cfg", "fp", "k", str(artifact), _TS))
    await db.commit()
    res = await reset_content(sid, db)
    assert res.get("ok") is True
    cur = await db.execute("SELECT content FROM sections WHERE scheme_id=?", (sid,))
    assert all((r[0] or "") == "" for r in await cur.fetchall()), "reset_content 未清空正文"
    cur = await db.execute("SELECT COUNT(*) AS c FROM export_cache WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())[0] == 0, "正文已重置但导出缓存未失效（孤儿行残留）"
    assert not artifact.exists(), "正文已重置但磁盘导出产物残留"


@pytest.mark.asyncio
async def test_reset_content_cache_failsoft_does_not_break_reset(db_ctx, monkeypatch):
    """缓存失效抛错不得阻断重置本身（fail-soft）。"""
    db, pid, sid = db_ctx
    await _insert_section(db, sid, pid, content="正文内容")
    import app.services.facts_extractor as fe

    async def _boom(*a, **k):
        raise RuntimeError("模拟缓存失效失败")

    monkeypatch.setattr(fe, "invalidate_export_cache", _boom)
    res = await reset_content(sid, db)
    assert res.get("ok") is True, "缓存失效失败时重置本身也必须成功"
    cur = await db.execute("SELECT content FROM sections WHERE scheme_id=?", (sid,))
    assert all((r[0] or "") == "" for r in await cur.fetchall())


# ---------------------------------------------------------------------------
# 4. 静态锁：防止上述修复回退（真正的 A/B 定向失败层）
# ---------------------------------------------------------------------------
def _calls_name(func_node: ast.AST, name: str) -> bool:
    """判断函数体内是否**真的调用**了名为 ``name`` 的函数。

    ⚠️ 用「字符串是否出现」当判据是**假守栏**：
    把调用删掉、只留下 `from ... import invalidate_export_cache` 这一行，
    字符串判据仍为真，而缓存实际没有被失效。A/B 实测（AB4）已确认
    这个盲点，故改用 ``ast.Call`` 判据 —— 只认真实调用表达式。
    """
    for sub in ast.walk(func_node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        target = (f.id if isinstance(f, ast.Name)
                  else f.attr if isinstance(f, ast.Attribute) else None)
        if target == name:
            return True
    return False



def _sections_content_writers() -> dict[str, bool]:
    """扫描 sections.py，返回 {函数名: 该函数内是否调用 invalidate_export_cache}。

    判据与风险同构：找 SQL 字面量里同时含 ``UPDATE sections`` 与 ``content=``
    的写语句，归属到所在函数；再看该函数源码是否出现
    ``invalidate_export_cache``。用 AST 而非纯文本，避免被注释/文档字符串误判。
    """
    src = (ROUTERS_DIR / "sections.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            parts: list[str]
            if isinstance(sub, ast.JoinedStr):
                parts = [v.value for v in sub.values if isinstance(v, ast.Constant)]
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                parts = [sub.value]
            else:
                continue
            text = "".join(parts)
            if "UPDATE sections" in text and re.search(r"\bcontent\s*=", text):
                out.setdefault(node.name, False)
                if _calls_name(node, "invalidate_export_cache"):
                    out[node.name] = True
    return out


#: 已知例外：跨入口共用的正文写 helper（它自己不该失效缓存，失效由调用方负责）。
#: 每个例外都必须由 test_shared_content_helper_callers_all_invalidate_cache 兜住，
#: 否则「调用方漏失效」就是无护栏的空洞。
_CONTENT_WRITER_HELPERS = {"_renormalize_all_section_contents"}


def test_every_sections_content_writer_invalidates_export_cache():
    """sections.py 的每个正文写路径都必须失效导出缓存（否则孤儿行残留）。"""
    writers = _sections_content_writers()
    missing = sorted(
        k for k, v in writers.items()
        if not v and k not in _CONTENT_WRITER_HELPERS)
    assert missing == [], (
        f"以下正文写路径漏接 invalidate_export_cache：{missing}。"
        "改写正文后指纹变化 -> 旧 export_cache 行永远不会再被命中，但既不失效也不裁剪，"
        "只能等 _prune_export_cache（5 份/方案）慢慢挤出；期间 /cache-status 仍把这些"
        "行报为 exists=true 的「有效缓存」，界面显示「可复用上次成果」。"
        "若它是跨入口共用的 helper，请登记进 _CONTENT_WRITER_HELPERS 并补调用方护栏。")
#: 静态锁：ORDER BY 只写 created_at（缺 rowid 次序兜底）的 SQL 片段
_CREATED_AT_ONLY = re.compile(
    r"ORDER\s+BY\s+(?:[A-Za-z_][\w]*\s*\.\s*)?created_at\s+DESC(?!\s*,\s*rowid)")



def test_shared_content_helper_callers_all_invalidate_cache():
    """共享正文写 helper 的**每个调用方**都必须失效导出缓存。

    helper 自己不失效是合理的（它被 create/update/delete/reorder/save-outline 五个
    结构变更入口共用），但任一入口漏失效，就等于该入口改了正文却留下孤儿缓存行。
    这是「helper 例外」不会变成空洞的关键护栏。
    """
    src = (ROUTERS_DIR / "sections.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    callers: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id in _CONTENT_WRITER_HELPERS
                    and sub.func.id != node.name):
                body_src = ast.get_source_segment(src, node) or ""
                callers.setdefault(node.name,
                                      _calls_name(node, "invalidate_export_cache"))
    assert callers, "未找到任何调用方 —— 判据失效，护栏等于空跑"
    missing = sorted(k for k, v in callers.items() if not v)


def _multiline_string_spans(src: str) -> list[tuple[int, int]]:
    """一次性收集所有**多行**字符串字面量（docstring / 长文本）的行区间。

    文档里举例 ``ORDER BY created_at DESC`` 说明问题背景时不是缺陷，必须排除。
    ⚠️ 只解析一次并缓存区间 —— 若逐行重跑 ``ast.parse``，5000 行源码 = 5000 次解析。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [(0, 10 ** 9)]
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and "\n" in node.value):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _in_multiline_string(src: str, lineno: int) -> bool:
    """判断 lineno 是否落在某个多行字符串字面量内（每次调用只走缓存的区间表）。"""
    return any(lo <= lineno <= hi for lo, hi in _MULTILINE_STR_CACHE[src])


#: 缓存：同一份源码只解析一次（导出路由 5000 行，逐行解析会挂死）。
_MULTILINE_STR_CACHE: dict[str, list[tuple[int, int]]] = {}


def _is_inside_comment(line: str, col: int) -> bool:
    """判断 col 是否落在行内注释里（避免把注释/文档里的示例误判成缺陷）。"""
    try:
        for tok in tokenize.generate_tokens(io.StringIO(line).readline):
            if tok.type == tokenize.COMMENT and tok.start[1] <= col:
                return True
    except tokenize.TokenError:
        return True
    return False


@pytest.mark.parametrize("fname", ["review.py", "export.py"])
def test_no_created_at_only_ordering_in_review_and_export(fname):
    """禁止 ``ORDER BY created_at DESC`` 无次序兜底（秒级精度 -> 次序未定义）。"""
    src = (ROUTERS_DIR / fname).read_text(encoding="utf-8")
    _MULTILINE_STR_CACHE[src] = _multiline_string_spans(src)
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        if _in_multiline_string(src, i):
            continue
        for m in _CREATED_AT_ONLY.finditer(line):
            if _is_inside_comment(line, m.start()):
                continue
            hits.append(f"L{i}: {line.strip()}")
    assert hits == [], (
        f"{fname} 存在只按 created_at 排序的查询（次序未定义）：{hits}\n"
        "created_at 是 TEXT 秒级精度，同秒写入的多行返回次序取决于查询计划："
        "取到旧行会造成假阻断 / 绕过门禁 / 分页重复或漏行 / 「最近 5 份缓存」取错。"
        "请补第二排序键 `rowid DESC`（rowid 单调递增 = 真实写入顺序）。")


# ---------------------------------------------------------------------------
# 5. 跨语言 parity：review.py 的 next_actions 必须是 _ALLOWED_TRANSITIONS 的**真实投影**
# ---------------------------------------------------------------------------

def _allowed_transitions_matrix():
    """动态读取 _ALLOWED_TRANSITIONS，构造 12x12 布尔矩阵。"""
    src = (ROUTERS_DIR / "review.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_ALLOWED_TRANSITIONS"
                        for t in node.targets)):
            mat = ast.literal_eval(node.value)
            assert mat, "_ALLOWED_TRANSITIONS 为空 —— 护栏失去基线"
            return mat
    raise AssertionError("未在 review.py 中找到 _ALLOWED_TRANSITIONS 定义")


_NEXT_ACTIONS_RE = re.compile(
    r"const\s+NEXT_ACTIONS\s*:\s*[^=]*=\s*\{(.*?)\n\};", re.S)
_NEXT_KEY_RE = re.compile(r'^\s*(?:"(?P<q>"|[^"]*)"|(?P<bare>[A-Za-z_]\w*))\s*:\s*\[')
_NEXT_ITEM_RE = re.compile(r'to:\s*"(?P<to>[a-z]+)",\s*label:\s*"(?P<label>[^"]*)"')


def _parse_frontend_next_actions() -> dict[str, dict[str, str]]:
    """解析 ReviewWorkflowPanel.tsx 的 NEXT_ACTIONS 字面量 -> ``{from: {to: label}}``。

    ⚠️ 本护栏必须**真的解析前端源码** —— 本仓此前的状态流转 parity 护栏是
    「手写镜像 vs 手写镜像」，两侧一起漂移时恒通过，等于没有护栏。
    """
    src = REVIEW_PANEL_TSX.read_text(encoding="utf-8")
    m = _NEXT_ACTIONS_RE.search(src)
    assert m, (
        "未能从 ReviewWorkflowPanel.tsx 解析 NEXT_ACTIONS 字面量 —— "
        "护栏失去基线（必须真的读到前端源码，不能退化成手写镜像）。"
    )
    out: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in m.group(1).splitlines():
        mk = _NEXT_KEY_RE.match(line)
        if mk:
            # 键可能是 `""`（未纳入审核）也可能是裸标识符
            current = (mk.group("q") if mk.group("q") is not None
                       else mk.group("bare"))
            out.setdefault(current, {})
        # ⚠️ 同一行既声明 key 又常带 items（`pending: [{ to: ... }]`），
        # 所以不能用 elif —— 否则只有折行 continuation 的 items 会被解析到。
        if current is not None:
            for mi in _NEXT_ITEM_RE.finditer(line):
                out[current][mi.group("to")] = mi.group("label")
    assert out, "解析出的 NEXT_ACTIONS 为空（正文结构已变？）"
    return out


# ---------------------------------------------------------------------------
# 6. 跨语言 parity：前端可点按钮 ⊆ 后端允许的流转（否则用户一点必 400）
# ---------------------------------------------------------------------------

_REVIEW_UI_STATES = ("", "pending", "reviewing", "approved", "rejected")


def test_frontend_next_actions_are_allowed_by_backend():
    """前端每个可点按钮都必须是后端允许的流转。

    后端 ``_ALLOWED_TRANSITIONS`` 是全连接矩阵（每个状态都能转到任一非空状态），
    前端只渲染其中一部分。方向必须单向：前端 ⊆ 后端。反向（前端有、后端没有）
    就是真实缺陷 —— 用户点击必然收到 400「不允许的流转」。
    """
    be = {f: set(v) for f, v in _allowed_transitions_matrix().items()}
    fe = _parse_frontend_next_actions()
    for frm, acts in fe.items():
        tos = set(acts)
        allowed = be.get(frm, set())
        bad = sorted(tos - allowed)
        assert not bad, (
            f"前端给 {frm!r} 渲染了后端禁止的流转按钮：{bad}\n"
            f"  前端: {sorted(tos)}\n  后端允许: {sorted(allowed)}\n"
            "用户点击这些按钮必然收到 400「不允许的流转」。"
        )


def test_frontend_next_actions_cover_every_status():
    """每个状态都必须至少有按钮入口（否则该状态在 UI 上成为死胡同）。"""
    fe = _parse_frontend_next_actions()
    missing = [s for s in _REVIEW_UI_STATES if not fe.get(s)]
    assert not missing, (
        f"前端 NEXT_ACTIONS 缺少这些状态的操作入口：{missing}\n"
        "该状态下的章节没有任何操作按钮，评审人卡住却无从得知原因。"
    )


def test_frontend_next_actions_no_empty_labels():
    """前端 NEXT_ACTIONS 的每个 label 都非空（空文案的按钮用户看不出可做什么）。"""
    actions = _parse_frontend_next_actions()
    bad = {f: to for f, acts in actions.items() for to, lb in acts.items()
           if not lb.strip()}
    assert bad == {}, f"前端 NEXT_ACTIONS 存在空 label：{bad}"


# ---------------------------------------------------------------------------
# A/B 反向验证索引（已执行，2026-10-06，均还原后字节一致并复跑全绿）
# ---------------------------------------------------------------------------
# A1  摘掉 submit_scheme_review 的 `, rowid DESC`        -> test_submit_picks_newest_row_when_created_at_ties 定向失败（读到 blocked 旧行 -> 422）
# A2  摘掉 get_review_summary 的 `, rowid DESC`          -> test_submit_gate_still_blocks_when_latest_row_blocked 定向失败（读到 released 旧行 -> 门禁被绕过）
# A3  摘掉 review_records 分页的 `, rowid DESC`          -> test_review_records_pagination_is_contiguous_when_created_at_ties 定向失败（跨页重复）
# A4  摘掉 cache_status 的 `, rowid DESC`                -> test_cache_status_returns_newest_rows_when_created_at_ties /
#                                                            test_cache_status_marks_missing_files_as_stale 定向失败
# A5  摘掉 reset_content 的 invalidate_export_cache      -> test_reset_content_invalidates_export_cache 定向失败（孤儿行）
#     注：行为锁 A1-A5 是**语义正确性**护栏；A1 已用探针实测确认「当前索引下次序恰好
#     稳定」，所以它只在查询计划变化时才真正报出用户可见错误 —— 静态锁
#     test_no_created_at_only_ordering_in_review_and_export 是能在 A/B 中**定向失败**
#     的那一层，两者互补。
# A6  摘掉 shrink_section 的 invalidate_export_cache     -> test_every_sections_content_writer_invalidates_export_cache 定向失败
# A7  把 _renormalize_all_section_contents 移出例外表    -> test_every_sections_content_writer_invalidates_export_cache 定向失败
#     （证明例外表不是「万能出口」；改回后由调用方护栏兜住）
#     + 把 5 个入口的 invalidate 摘掉                     -> test_shared_content_helper_callers_all_invalidate_cache 定向失败
# A8  前端 NEXT_ACTIONS 与后端矩阵漂移                    -> test_frontend_next_actions_are_exact_projection_of_backend_matrix
#     / test_frontend_next_actions_parity 定向失败（本仓此前该护栏是「手写镜像 vs 手写
#     镜像」，永不失败 —— 见下方 _parse_frontend_next_actions 的说明）
# ---------------------------------------------------------------------------

