"""R209（2026-11-10）· 目录生成模块深度探索增强 —— 新增护栏。

覆盖本轮落地的修复项：

- #1（P1）目录库套用：同名节点注入重复 ``__original_id`` → 整表保存 400 失败
  （孪生实现分歧：``upload_outline.save_as_outline`` 用「消费式」匹配 +
  ``consumed`` 集合，``outline_library.apply_library_and_save`` 用「不消费的
  标题字典」⇒ 同名子节（标准骨架里「施工准备」出现在多章下是常态）必然撞主键）。
- #2（P2）目录库套用：标题匹配未按层级/路径收敛 ⇒ 跨层级同名章节被错配，
  正文被搬到结构不对应的章节上。
- #3（P3）``renumber_section_outline_ids`` 的 ``visited`` 是**全树共享**而非
  路径级 ⇒ 同一节点在树中出现两次（共享子树）时第二次被跳过 → 编号缺号。
"""

import asyncio
import json

import aiosqlite
import pytest
from app.db import _migrate, _setup_connection
from app.routers.outline_library import apply_library_and_save
from app.schema_sql import SCHEMA_SQL

SCHEME = "s-r209-lib"
PROJECT = "p-r209-lib"


async def _make_db(tmp_path, sections: list[dict]):
    """建库并写入初始章节；返回连接。"""
    path = tmp_path / "r209_lib.sqlite3"
    setup = await aiosqlite.connect(path)
    await setup.executescript(SCHEMA_SQL)
    await _migrate(setup)
    await setup.execute("INSERT OR IGNORE INTO projects(id,name) VALUES(?,?)", (PROJECT, "目录库套用验证"))
    await setup.execute(
        "INSERT OR IGNORE INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,?)",
        (SCHEME, PROJECT, "目录库套用验证", "草稿", 30000),
    )
    for s in sections:
        await setup.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order,"
            " content, word_count, word_budget) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                s["id"],
                SCHEME,
                PROJECT,
                s.get("parent", ""),
                s["title"],
                s.get("level", 1),
                s.get("order", 0),
                s.get("content", ""),
                s.get("word_count", 0),
                s.get("word_budget", 1200),
            ),
        )
    await setup.commit()
    await setup.close()

    conn = await aiosqlite.connect(path)
    await _setup_connection(conn)
    return conn


def _library_outline(nodes: list[dict]) -> str:
    return json.dumps(nodes, ensure_ascii=False)


def _lib(title: str, children: list[dict] | None = None) -> dict:
    return {"id": "", "title": title, "children": children or []}


@pytest.mark.asyncio
async def test_apply_library_same_title_nodes_does_not_400(tmp_path):
    """#1 同名子节不得触发「重复章节主键」400。

    目录库标准骨架里「施工准备」同时出现在多章之下是常态；旧实现按
    「标题 → 首个 DB 主键」建立字典且**不消费**，两个同名节点被注入**同一个**
    ``__original_id`` ⇒ ``_save_outline_to_db`` 的重复主键闸门 400，
    「套用目录库」整条链直接失败。
    """
    conn = await _make_db(tmp_path, [{"id": "sec-old-1", "title": "施工准备", "content": "旧正文", "word_count": 4}])
    try:
        outline = _library_outline(
            [
                _lib("第一章", [_lib("施工准备")]),
                _lib("第二章", [_lib("施工准备")]),
            ]
        )
        await conn.execute(
            "INSERT INTO outline_library (id,name,outline_json,review_status) VALUES(?,?,?,?)",
            ("lib-r209", "含同名子节的目录库", outline, "已通过"),
        )
        await conn.commit()

        result = await apply_library_and_save("lib-r209", {"scheme_id": SCHEME}, conn)

        assert result.get("ok") is True
        # 两个同名节点各自成行（一条保留旧正文、一条新建）
        cur = await conn.execute(
            "SELECT title, content FROM sections WHERE scheme_id=? AND title='施工准备' ORDER BY id",
            (SCHEME,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 2, f"同名节点应各自成行，实际 {rows}"
        assert any(r["content"] == "旧正文" for r in rows), "旧正文应被保留"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_apply_library_same_title_nodes_in_scheme_not_400(tmp_path):
    """#1 变体：方案自身含同名章节时，套用同名目录库也不得 400。

    目录库只有 1 个「施工准备」节点 ⇒ 新目录只有 1 章同名 ⇒ 旧的两个同名
    章节中必然有一个要下线（正文随之清除，且由 cleared_content_sections
    如实回传）。本用例锁定的是：**不得 400、不得静默丢正文**。
    """
    conn = await _make_db(
        tmp_path,
        [
            {"id": "sec-a", "title": "施工准备", "content": "正文A", "word_count": 3},
            {"id": "sec-b", "title": "施工准备", "content": "正文B", "word_count": 3},
        ],
    )
    try:
        outline = _library_outline([_lib("施工准备"), _lib("安全管理")])
        await conn.execute(
            "INSERT INTO outline_library (id,name,outline_json,review_status) VALUES(?,?,?,?)",
            ("lib-r209-b", "同名目录库", outline, "已通过"),
        )
        await conn.commit()

        result = await apply_library_and_save("lib-r209-b", {"scheme_id": SCHEME}, conn)

        assert result.get("ok") is True
        cur = await conn.execute(
            "SELECT content FROM sections WHERE scheme_id=? AND title='施工准备'", (SCHEME,)
        )
        rows = [dict(r) for r in await cur.fetchall()]
        # 新目录只有 1 个「施工准备」⇒ 旧行只保留 1 条，且正文必须是两份之一
        assert len(rows) == 1, f"新目录只含 1 个同名节点，实际 {rows}"
        assert rows[0]["content"] in ("正文A", "正文B"), rows
        # 被下线的那个必须如实计数，不得静默消失
        assert result.get("cleared_content_sections") == 1, result
        # 另一个节点正常新建
        cur = await conn.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id=? AND title='安全管理'", (SCHEME,)
        )
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_apply_library_preserves_content_by_position(tmp_path):
    """#2 标题匹配必须收敛到「结构同形」的旧章节（标题路径优先）。

    旧实现只按标题全局匹配：方案里二级章节「施工准备」与另一个一级章下的
    三级章节同名时，套用后旧章节可能被互换，正文随章节漂移
    （用户看到「A 章的正文出现在 B 章」）。

    目录库结构：施工部署 > 施工准备（L2）。
    旧方案：工程概况 > 施工准备（L2） / 施工部署 > 施工准备（L3）。
    标题路径相同的是「施工部署 > 施工准备」⇒ 必须认领 **sec-l3**，
    而非同名但路径不同的 **sec-l2**。
    """
    conn = await _make_db(
        tmp_path,
        [
            {"id": "sec-l1", "title": "工程概况", "level": 1, "order": 0},
            {"id": "sec-l2", "title": "施工准备", "level": 2, "order": 0, "parent": "sec-l1"},
            {"id": "sec-l1b", "title": "施工部署", "level": 1, "order": 1},
            {"id": "sec-l3", "title": "施工准备", "level": 3, "order": 0, "parent": "sec-l1b"},
        ],
    )
    try:
        # 目录库：一级「施工部署」下挂二级「施工准备」
        outline = _library_outline([_lib("施工部署", [_lib("施工准备")])])
        await conn.execute(
            "INSERT INTO outline_library (id,name,outline_json,review_status) VALUES(?,?,?,?)",
            ("lib-r209-c", "结构目录库", outline, "已通过"),
        )
        await conn.commit()

        await apply_library_and_save("lib-r209-c", {"scheme_id": SCHEME}, conn)

        cur = await conn.execute(
            "SELECT id, title, level, parent_id FROM sections WHERE scheme_id=? ORDER BY title, id",
            (SCHEME,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        titles = {r["title"] for r in rows}
        # 目录库里没有「工程概况」⇒ 它随整表重建下线（预期）
        assert titles == {"施工准备", "施工部署"}, rows
        prep = [r for r in rows if r["title"] == "施工准备"][0]
        # ✅ 认领的是**标题路径相同**的 sec-l3（不是同名但路径不同的 sec-l2）
        assert prep["id"] == "sec-l3", f"应认领标题路径相同的旧章节，实际 {prep}"
        # 层级跟随目录库结构（L2），父级为「施工部署」
        assert prep["level"] == 2, prep
        parent = [r for r in rows if r["id"] == prep["parent_id"]]
        assert parent and parent[0]["title"] == "施工部署", f"父级应仍为「施工部署」：{prep}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_save_outline_import_keys_preserve_content(tmp_path):
    """#2（核心）前端「导入目录 / AI 调整目录」保存不得清空已有正文。

    前端导入结果的节点 key 是 ``upload_<位置路径>`` 临时键（无 DB 主键），
    旧实现只认 ``__original_id`` ⇒ 全部 is_new ⇒ 旧章节进 to_delete ⇒
    **已生成正文被整批级联删除**（用户表现为「导入一次目录，之前写的正文全没了」）。
    现按标题路径兜底匹配认领旧章节，正文保留。
    """
    from app.routers.sections import _save_outline_to_db

    conn = await _make_db(
        tmp_path,
        [
            {"id": "sec-1", "title": "工程概况", "content": "概况正文", "word_count": 4},
            {"id": "sec-2", "title": "施工部署", "content": "部署正文", "word_count": 4},
        ],
    )
    try:
        tree = [
            {"id": "upload_1", "title": "工程概况", "children": []},
            {"id": "upload_2", "title": "施工部署", "children": []},
        ]
        result = await _save_outline_to_db(conn, SCHEME, tree, source="导入识别")
        assert result.get("ok") is True
        assert not result.get("cleared_content_sections"), result
        cur = await conn.execute(
            "SELECT title, content FROM sections WHERE scheme_id=? ORDER BY title", (SCHEME,)
        )
        rows = {dict(r)["title"]: dict(r)["content"] for r in await cur.fetchall()}
        assert rows.get("工程概况") == "概况正文", rows
        assert rows.get("施工部署") == "部署正文", rows
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_save_outline_local_new_node_is_not_merged(tmp_path):
    """#2 反例：用户手工新增（``local_`` 前缀）的同名节点**不得**被合并进旧章节。

    兜底匹配只服务于「没有 DB 主键的导入/调整/模板」三类来源；``local_``
    是用户「新增章节」的明确意图，标题相同也应新建（否则新增同名章节会被
    悄悄合并，正文错位）。
    """
    from app.routers.sections import _save_outline_to_db

    conn = await _make_db(
        tmp_path,
        [{"id": "sec-1", "title": "施工准备", "content": "旧正文", "word_count": 3}],
    )
    try:
        tree = [
            {"id": "sec-1", "title": "施工准备", "children": []},
            {"id": "local_x", "title": "施工准备", "children": []},
        ]
        result = await _save_outline_to_db(conn, SCHEME, tree, source="手动编辑")
        assert result.get("ok") is True
        cur = await conn.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id=? AND title='施工准备'", (SCHEME,)
        )
        assert (await cur.fetchone())[0] == 2, "手工新增的同名章节应独立成行"
    finally:
        await conn.close()


def test_renumber_section_outline_ids_skips_repeated_id():
    """#3 同一 DB 主键在树中重复出现时**只编号一次**（避免一行被写两个编号）。

    ``renumber_section_outline_ids`` 的 visited 是**全树共享**的：sections
    的一行只有一个 parent_id，DB 树不可能出现同一行的两个挂点；共享子树
    只会来自手工构造的树。此时若按「路径级 visited」处理，同一行会被产出
    两个不同编号（"1.1" 与 "2.1"），后写者覆盖前写者 ⇒ 该行的编号与其真实
    位置错位（比「缺号」更糟）。故现状「全局 visited、只编号一次」是**正确**
    语义，此用例把该意图钉住（R209 探查曾误判为缺陷，此处证伪）。
    """
    from app.services.numbering import renumber_section_outline_ids

    shared = {"id": "shared", "outline_json": json.dumps({"id": "9"}), "children": []}
    tree = [
        {"id": "a", "outline_json": json.dumps({"id": "1"}), "children": [shared]},
        {"id": "b", "outline_json": json.dumps({"id": "2"}), "children": [shared]},
    ]
    updates = renumber_section_outline_ids(tree)
    ids = [json.loads(u[0])["id"] for u in updates]
    # shared 只出现一次（首次位置），且不会产出第二个编号
    assert ids.count("1.1") == 1, ids
    assert "2.1" not in ids, ids


# ---------------------------------------------------------------------------
# #4 空标题落库闸门
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_outline_rejects_empty_title(tmp_path):
    """#4 空标题必须被落库闸门拦下（400），不得静默入库成无名章节。"""
    from app.routers.sections import _save_outline_to_db
    from fastapi import HTTPException

    conn = await _make_db(tmp_path, [])
    try:
        tree = [{"id": "local_a", "title": "工程概况", "children": [{"id": "local_b", "title": "   ", "children": []}]}]
        with pytest.raises(HTTPException) as ei:
            await _save_outline_to_db(conn, SCHEME, tree, source="手动编辑")
        assert ei.value.status_code == 400, ei.value
        cur = await conn.execute("SELECT COUNT(*) FROM sections WHERE scheme_id=?", (SCHEME,))
        assert (await cur.fetchone())[0] == 0, "被拒的目录不得留下任何行"
    finally:
        await conn.close()


def test_validate_outline_numbering_reports_empty_title():
    """#4 纯函数层判据：空标题进 problems，且与标题超长同口径。"""
    from app.services.outline_utils import validate_outline_numbering

    problems = validate_outline_numbering([{"id": "1", "title": "", "children": []}])
    assert any("标题为空" in p for p in problems), problems
    ok = validate_outline_numbering([{"id": "1", "title": "工程概况", "children": []}])
    assert ok == [], ok


# ---------------------------------------------------------------------------
# #5 罗马数字编号形态（导入目录少一层）
# ---------------------------------------------------------------------------


def test_simple_parse_outline_supports_roman_numerals():
    """#5 罗马数字编号行不得整行漏识（此前 "I. 总则" 整行被丢弃）。"""
    from app.services.file_parser import simple_parse_outline

    out = simple_parse_outline("I. 总则\nII. 施工部署\nⅢ. 验收要求\n")
    titles = [n["title"] for n in out]
    assert titles == ["总则", "施工部署", "验收要求"], out
    assert [n["level"] for n in out] == [1, 1, 1], out


def test_simple_parse_outline_roman_not_false_positive():
    """#5 反例：英文句子 "I am a worker" 不得被当成编号行。"""
    from app.services.file_parser import simple_parse_outline

    out = simple_parse_outline("I am a worker on site\nThis is a normal sentence.\n")
    assert out == [], out


# ---------------------------------------------------------------------------
# #6 快照回退清理孤儿图表登记
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_snapshot_clears_chart_predictions(tmp_path):
    """#6 快照回退是整表重建 —— 必须同步清理 chart_predictions，否则留下孤儿行。"""
    from app.services.outline_versions import create_outline_snapshot, restore_outline_snapshot

    conn = await _make_db(tmp_path, [{"id": "sec-1", "title": "工程概况", "content": "正文"}])
    try:
        await conn.execute(
            "INSERT INTO chart_predictions (id, scheme_id, section_id, chart_type) VALUES(?,?,?,?)",
            ("cp-1", SCHEME, "sec-1", "mermaid"),
        )
        await conn.commit()
        snap_id = await create_outline_snapshot(conn, SCHEME, snapshot_type="test", commit=True)
        await restore_outline_snapshot(conn, SCHEME, snap_id)
        cur = await conn.execute("SELECT COUNT(*) FROM chart_predictions WHERE scheme_id=?", (SCHEME,))
        assert (await cur.fetchone())[0] == 0, "回退后不得残留孤儿图表登记"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# #7 拖拽重排少行校验（并发删除 → 409，不得静默只应用一半）
# ---------------------------------------------------------------------------


class _RaceConn:
    """在拖拽重排的批量 UPDATE 之前偷偷删掉一个章节，模拟并发删除。"""

    def __init__(self, conn, victim: str):
        self._conn = conn
        self._victim = victim

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("UPDATE SECTIONS SET SORT_ORDER"):
            await self._conn.execute("DELETE FROM sections WHERE id=?", (self._victim,))
            await self._conn.commit()
        return await self._conn.execute(sql, *args, **kwargs)

    async def executemany(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("UPDATE SECTIONS SET SORT_ORDER"):
            await self._conn.execute("DELETE FROM sections WHERE id=?", (self._victim,))
            await self._conn.commit()
        return await self._conn.executemany(sql, *args, **kwargs)


@pytest.mark.asyncio
async def test_reorder_rejects_partial_apply(tmp_path):
    """#7 并发删除导致命中行数不足时，必须 409 中止而不是「静默只排了一半」。"""
    from app.routers.sections import reorder_sections
    from fastapi import HTTPException

    conn = await _make_db(
        tmp_path,
        [
            {"id": "sec-1", "title": "甲", "order": 0},
            {"id": "sec-2", "title": "乙", "order": 1},
        ],
    )
    try:
        race = _RaceConn(conn, "sec-2")
        with pytest.raises(HTTPException) as ei:
            await reorder_sections(SCHEME, {"order": ["sec-1", "sec-2"]}, race)
        assert ei.value.status_code == 409, ei.value
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# #8 上传目录落库写锁（并发整表重建）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_as_outline_takes_write_lock(tmp_path):
    """#8 上传目录落库（整表重建）必须先取写锁（与 save-outline 同口径）。"""
    from app.routers.upload_outline import save_as_outline

    conn = await _make_db(tmp_path, [{"id": "sec-1", "title": "工程概况", "content": "正文"}])
    executed: list[str] = []

    class _Spy:
        def __init__(self, c):
            self._c = c

        def __getattr__(self, name):
            return getattr(self._c, name)

        async def execute(self, sql, *args, **kwargs):
            executed.append(sql.strip().upper())
            return await self._c.execute(sql, *args, **kwargs)

    try:
        await conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, file_name, file_type, raw_text,"
            " parsed_json, status) VALUES(?,?,?,?,?,?,?,?)",
            ("up-1", PROJECT, SCHEME, "a.docx", "docx", "", "{}", "parsed"),
        )
        await conn.commit()
        try:
            await save_as_outline(
                "up-1",
                {"scheme_id": SCHEME, "outline": [{"title": "工程概况", "children": []}]},
                _Spy(conn),
            )
        except Exception:  # noqa: BLE001 — 后续断言只关心是否取过写锁
            pass
        assert any(s.startswith("BEGIN IMMEDIATE") for s in executed), executed[:20]
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# #9 L1 章号超 30 的渲染回退（导出侧不得产出重复章号）
# ---------------------------------------------------------------------------


def test_heading_v2_l1_over_30_falls_back_to_arabic():
    """#9 目录含 ≥31 个一级章时，导出标题不得全部塌缩为「第三十章」。

    旧实现 ``min(30, c[0])`` ⇒ 第 31/32/… 章标题**字面相同**，且三章的
    正文子标题前缀全部是 "30"（跨章撞号）。现与 numbering.stored_id_to_display
    与前端 formatOutlineTitle 同口径：字表外回退阿拉伯数字。
    """
    from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2

    gen = HeadingNumberingGeneratorV2()
    seen = []
    for _i in range(32):
        # 同一 parent_id ⇒ L1 计数器连续递增（换 parent 会重置，见 update_counter）
        seen.append(gen.update_counter(1, "root"))
    assert seen[29] == "第三十章", seen[28:31]
    assert seen[30] == "第31章", seen[28:31]
    assert seen[31] == "第32章", seen[28:31]
    assert len(set(seen)) == 32, "32 个一级章必须产出 32 个互不相同的章号"


def test_heading_v2_matches_numbering_display():
    """#9 导出侧与 numbering 唯一事实源逐档一致（1..32 章）。"""
    from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
    from app.services.numbering import stored_id_to_display

    gen = HeadingNumberingGeneratorV2()
    for i in range(1, 33):
        got = gen.update_counter(1, "root")
        want = stored_id_to_display(str(i))
        assert got == want, f"第 {i} 章：导出={got} vs numbering={want}"


# ---------------------------------------------------------------------------
# #10 编排提示词章节编号（改名残留漏改）
# ---------------------------------------------------------------------------


def test_content_plan_section_number_not_empty():
    """#10 编排提示词的「【章节编号】」不得恒为空串。

    R182 把生成期编号字段改名为 ``_prompt_number``，``content_plan.py``
    未同步（仍读不存在的 ``outline_number`` / ``number``）⇒ 编排 LLM 拿不到
    章号。现按现行字段名读取，并补本护栏防再次改名漏改。
    """
    import inspect

    from app.services import content_plan

    src = inspect.getsource(content_plan)
    assert '_prompt_number' in src, "content_plan 必须读 _prompt_number（生成期编号字段真名）"


# ---------------------------------------------------------------------------
# #11 目录库入库闸门（标题超长 / 空标题 / 不可见字符）
# ---------------------------------------------------------------------------


def test_normalize_outline_for_library_rejects_long_title():
    """#11 目录库入库即拒绝超长标题（不再「库里能存、套用时被拒」）。"""
    from app.services.outline_utils import normalize_outline_for_library

    long_title = "工" * 250
    js, problems = normalize_outline_for_library([{"title": long_title, "children": []}])
    assert problems, "超长标题必须在入库时被拦下"
    assert any("200" in p for p in problems), problems
    assert js  # 仍返回规范化结果，便于调用方给出错误上下文


def test_normalize_outline_for_library_strips_invisible_chars():
    """#11 零宽/控制字符标题在入库前即被清洗（不再随目录库导出进 .md/.txt）。"""
    from app.services.outline_utils import normalize_outline_for_library

    js, problems = normalize_outline_for_library([{"title": "工程概况\u200b\ufeff\u2060", "children": []}])
    assert not problems, problems
    assert "\u200b" not in js and "\ufeff" not in js and "\u2060" not in js, js
    assert "工程概况" in js


@pytest.mark.asyncio
async def test_create_library_rejects_long_title(tmp_path):
    """#11 端点级：新建目录库时超长标题直接 400（错误归因正确）。"""
    from app.routers.outline_library import create_library
    from fastapi import HTTPException

    class _Payload:
        outline_json = [{"title": "工" * 250, "children": []}]
        name = "超长标题目录库"
        type = ""
        engineering_type = ""
        profession = ""
        applicable_conditions = ""
        basis = ""
        tags = ""
        source = "手动创建"

    conn = await _make_db(tmp_path, [])
    try:
        with pytest.raises(HTTPException) as ei:
            await create_library(_Payload(), conn)
        assert ei.value.status_code == 400, ei.value
        assert "200" in str(ei.value.detail), ei.value.detail
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# #12 正文 L7 字母编号自第 53 项起塌缩（R203b #18 同族漏改收口）
# ---------------------------------------------------------------------------


def test_alpha_label_is_excel_continuation():
    """#12 唯一实现：Excel 续编，1..52 与旧 ALPHABET 逐字相同，53 起不重复。"""
    from app.services.numbering import ALPHABET, alpha_label

    for i in range(1, 53):
        assert alpha_label(i) == ALPHABET[i - 1], f"第 {i} 项必须与旧表一致"
    assert alpha_label(52) == "az"
    assert alpha_label(53) == "ba"
    assert alpha_label(60) == "bh"
    assert len({alpha_label(i) for i in range(1, 200)}) == 199, "199 项不得出现重复"


def test_body_subheading_l7_does_not_collapse_at_53():
    """#12 正文子标题 L7 字母不得自第 53 项起塌缩为同一个字母。

    旧实现（content_blocks._compute_subheading 用 52 项 ALPHABET 钳制）
    ⇒ 第 53 项起全部输出 "az、" ⇒ 成稿出现多个同号子标题。
    """
    from app.services.content_blocks import _compute_subheading

    sub_counters: dict = {}
    # 首个 H2 标题建立基准层（rel=0 → L6 数字），其后 H3 为 rel=1 → L7 字母
    _compute_subheading("2.1", 2, 2, sub_counters, "基准", has_children=True)
    labels = []
    for i in range(60):
        text, _style = _compute_subheading(
            "2.1",
            2,
            3,
            sub_counters,
            f"小标题{i}",
            has_children=True,
        )
        labels.append(text.split("、")[0])
    assert len(set(labels)) == 60, f"L7 字母必须互不相同，实际塌缩：{labels[48:]}"
    assert labels[51] == "az", labels[48:]
    assert labels[52] == "ba", labels[48:]


def test_l7_implementations_share_single_source():
    """#12 三处 L7 实现必须共用 services.numbering.alpha_label（防再次同族漏改）。

    ⚠️ 静态判据走 **AST**（只判代码里的名字引用），不按字符串全文匹配 ——
    否则本轮注释里引述的旧字面量（``len(ALPHABET) - 1``）会让护栏恒红
    （AGENTS §5.14：静态源码锁被注释误伤）。
    """
    import ast
    import inspect

    from app.services import content_blocks
    from app.services.ai import heading_templates, heading_v2

    def _names_used(mod) -> set[str]:
        tree = ast.parse(inspect.getsource(mod))
        return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    for mod in (heading_templates, heading_v2):
        src = inspect.getsource(mod)
        assert "alpha_label" in src, f"{mod.__name__} 必须引用统一实现"
        assert "x % 26" not in src, f"{mod.__name__} 仍自持一份 Excel 续编实现"
    # 正文侧：代码里不得再出现 ALPHABET 名字引用（注释引述不算）
    assert "ALPHABET" not in _names_used(content_blocks), "content_blocks 仍在钳制 ALPHABET 索引"
    assert "alpha_label" in inspect.getsource(content_blocks), "content_blocks 的 L7 必须调用统一实现"


# ---------------------------------------------------------------------------
# #13 超三级目录树的折叠信号（加法式回传，不阻断保存）
# ---------------------------------------------------------------------------


def test_outline_depth_stats():
    """#13 折叠取样：返回实际最大深度与超限节点数。"""
    from app.services.outline_utils import outline_depth_stats

    tree = [
        {
            "title": "第一章",
            "children": [
                {"title": "1.1", "children": [{"title": "1.1.1", "children": [{"title": "1.1.1.1"}]}]},
            ],
        }
    ]
    depth, deep = outline_depth_stats(tree)
    assert depth == 4, depth
    assert deep == 1, deep  # 第 4 级节点被折叠


@pytest.mark.asyncio
async def test_save_outline_reports_clamped_depth(tmp_path):
    """#13 超三级目录被折叠时，响应必须如实回传（加法式，不阻断保存）。"""
    from app.routers.sections import _save_outline_to_db

    conn = await _make_db(tmp_path, [])
    try:
        tree = [
            {
                "id": "local_1",
                "title": "第一章",
                "children": [
                    {
                        "id": "local_2",
                        "title": "1.1",
                        "children": [
                            {
                                "id": "local_3",
                                "title": "1.1.1",
                                "children": [{"id": "local_4", "title": "1.1.1.1", "children": []}],
                            }
                        ],
                    }
                ],
            }
        ]
        result = await _save_outline_to_db(conn, SCHEME, tree, source="手动编辑")
        assert result.get("ok") is True
        assert result.get("clamped_depth") == 4, result
        assert result.get("clamped_nodes") == 1, result
    finally:
        await conn.close()


# ===========================================================================
# R209 · 断点续跑三件套（P1-1 checkpoint 归属 / P1-2 漏章 / P1-3 章序错乱）
# ===========================================================================


class TestOutlineResumeR209:
    """目录生成「断点续跑」（R203b #21）的三处缺陷。

    - **P1-1 归属错**：续跑读的是**本次** ``task_id`` 的 checkpoint，而
      ``task_registry.register_task`` 每次请求都生成新 uuid ⇒ 上次成果落在
      **上次任务行**上，本次恒读到空 ⇒ 续跑从未生效（R203b #21 的验收锚
      「日志出现断点续跑」在生产零命中）。
    - **P1-2 漏章**：续跑章的过滤发生在**单元切片之后**，且「本轮首单元全已
      续跑即 ``continue``」短路了窗口扩张 ⇒ 本轮后半程的章永不被调度。
    - **P1-3 章序错乱**：续跑章在预填充阶段先入列、非续跑章在批循环里后入列
      ⇒ ``full_outline`` 物理顺序 ≠ 章序；而编号由**位置**推导 ⇒ 整树编号错乱。
    """

    # ---------------- DB 层：续跑取数入口 ----------------

    @staticmethod
    async def _insert_task(conn, tid: str, scheme_id: str, status: str, checkpoint, updated_at="2026-11-10T00:00:00"):
        await conn.execute(
            "INSERT OR REPLACE INTO task_registry "
            "(id, task_type, project_id, scheme_id, status, progress, checkpoint_json, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                tid,
                "outline_generation",
                "p-r209",
                scheme_id,
                status,
                0.5,
                checkpoint if isinstance(checkpoint, str) else json.dumps(checkpoint, ensure_ascii=False),
                updated_at,
            ),
        )
        await conn.commit()

    @staticmethod
    def _ckpt(titles):
        return {
            "kind": "outline_result",
            "event": "stopped",
            "partial": True,
            "outline": [
                {"title": t, "children": [{"title": f"{t}-小节", "children": []}]} for t in titles
            ],
        }

    @pytest.mark.asyncio
    async def test_resumable_checkpoint_reads_interrupted_task_only(self, db_conn):
        """P1-1：只取「被中断」的任务成果；completed / 其他方案不得被复用。"""
        from app.routers.sse_checkpoint import _load_resumable_outline_checkpoint

        sid = "scheme-resume-1"
        await self._insert_task(db_conn, "t-stopped", sid, "stopped", self._ckpt(["第1章"]))
        await self._insert_task(
            db_conn, "t-completed", sid, "completed", self._ckpt(["第9章"]), "2026-11-10T01:00:00"
        )
        await self._insert_task(db_conn, "t-other", "scheme-elsewhere", "stopped", self._ckpt(["第8章"]))

        got = await _load_resumable_outline_checkpoint(sid)
        assert got is not None, "被中断任务的成果必须可取（否则续跑永远不生效）"
        assert got["kind"] == "outline_result"
        titles = [n.get("title") for n in got["outline"]]
        assert titles == ["第1章"], titles
        assert "第9章" not in titles, "completed 任务的成果不得被复用（重新生成必须全量重算）"
        assert "第8章" not in titles, "其他方案的成果不得被复用"

    @pytest.mark.asyncio
    async def test_resumable_checkpoint_excludes_self_and_handles_dirty_rows(self, db_conn):
        """P1-1：排除本次任务；脏 JSON / 异 kind / 空 outline 一律跳过且不抛。"""
        from app.routers.sse_checkpoint import _load_resumable_outline_checkpoint

        sid = "scheme-resume-2"
        await self._insert_task(db_conn, "t-self", sid, "stopped", self._ckpt(["第1章"]))
        await self._insert_task(db_conn, "t-dirty", sid, "stopped", "{not-json", "2026-11-10T02:00:00")
        await self._insert_task(
            db_conn,
            "t-otherkind",
            sid,
            "stopped",
            {"kind": "content_result", "outline": [1]},
            "2026-11-10T03:00:00",
        )
        await self._insert_task(
            db_conn, "t-empty", sid, "stopped", {"kind": "outline_result", "outline": []}, "2026-11-10T04:00:00"
        )

        # 排除自身 ⇒ 无可用来源（None），且脏行不得抛异常
        assert await _load_resumable_outline_checkpoint(sid, exclude_task_id="t-self") is None
        # 不排除自身 ⇒ 取到 t-self 的成果（脏行在其后被跳过）
        got = await _load_resumable_outline_checkpoint(sid)
        assert got is not None and [n.get("title") for n in got["outline"]] == ["第1章"]

    # ---------------- 端到端：续跑行为 ----------------

    async def _run_outline(self, db_conn, monkeypatch, *, total, resumed_titles, window=2, merge_k=1):
        """跑一次长方案分步目录生成，返回 (被调用的章序号, SSE 事件列表)。"""
        from app.routers import sse_handlers as sh

        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
        monkeypatch.setattr(sh, "OUTLINE_CHAPTER_CONCURRENCY", window)
        monkeypatch.setattr(sh, "OUTLINE_CHAPTER_BATCH_SIZE", merge_k)

        calls: list[int] = []

        async def fake_fetch_unit(unit_chapters, *, batch_prompt=None, single_prompts=None,
                                  task_id=None, sem=None, timeout=60, push_stats=None, **kw):
            calls.extend(i for i, _ in unit_chapters)
            per = [("ok", [{"title": f"第{i + 1}章-小节{i + 1}.1", "children": []}]) for i, _ in unit_chapters]
            return "ok", per

        async def fake_collect(messages, validate_fn=None, **kwargs):
            outline = [{"title": f"第{i}章", "description": "", "children": []} for i in range(1, total + 1)]
            return ({"outline": outline}, json.dumps({"outline": outline}, ensure_ascii=False))

        async def fake_review(outline, *a, **kw):
            return outline, {}

        monkeypatch.setattr(sh, "_fetch_unit_children", fake_fetch_unit)
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        monkeypatch.setattr(sh, "_review_and_fix_outline", fake_review)

        import uuid as _uuid

        sid, pid = _uuid.uuid4().hex, _uuid.uuid4().hex
        await db_conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "R209 续跑验证"))
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name, word_budget, config_json) VALUES (?,?,?,?,?)",
            (sid, pid, "R209 断点续跑验证", 10000, json.dumps({}, ensure_ascii=False)),
        )
        await db_conn.commit()
        if resumed_titles:
            await self._insert_task(db_conn, "t-old-r209", sid, "stopped", self._ckpt(resumed_titles))

        resp = await sh.generate_outline(sid, None, db_conn)
        events: list[dict] = []
        async for chunk in resp.body_iterator:
            for line in (chunk if isinstance(chunk, str) else chunk.decode("utf-8")).splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
        return calls, events

    @pytest.mark.asyncio
    async def test_resume_reuses_chapters_and_keeps_chapter_order(self, db_conn, monkeypatch):
        """P1-1 + P1-3：续跑章跳过 AI 调用，且最终目录**按章序**、编号按位置。"""
        calls, events = await self._run_outline(
            db_conn, monkeypatch, total=4, resumed_titles=["第1章", "第3章"]
        )
        assert calls == [1, 3], f"只有未续跑的章才允许发起 AI 调用，实际 {calls}"
        last = events[-1]
        assert last["event"] == "completed", [e.get("event") for e in events]
        titles = [n.get("title") for n in last["outline"]]
        assert titles == ["第1章", "第2章", "第3章", "第4章"], f"章序必须与 L1 一致，实际 {titles}"
        ids = [n.get("id") for n in last["outline"]]
        assert ids == ["1", "2", "3", "4"], f"编号由位置推导，章序错乱会整树编号错乱，实际 {ids}"

    @pytest.mark.asyncio
    async def test_resume_does_not_drop_chapters_when_first_unit_fully_resumed(self, db_conn, monkeypatch):
        """P1-2：本轮首单元「全已续跑」时，窗口扩张不得被短路 ⇒ 后续章不得漏。"""
        calls, events = await self._run_outline(
            db_conn, monkeypatch, total=4, resumed_titles=["第1章"], window=2, merge_k=1
        )
        assert calls == [1, 2, 3], f"未续跑的章必须全部被调度一次，实际 {calls}"
        assert len(calls) == len(set(calls)), f"同一章不得被调度两次（窗口越界会重复调度），实际 {calls}"
        titles = [n.get("title") for n in events[-1]["outline"]]
        assert titles == ["第1章", "第2章", "第3章", "第4章"], f"不得漏章，实际 {titles}"

    @pytest.mark.asyncio
    async def test_no_previous_task_means_full_regeneration(self, db_conn, monkeypatch):
        """P1-1 反例：没有可续跑来源时，行为逐字不变（每章都发起 AI 调用）。"""
        calls, events = await self._run_outline(db_conn, monkeypatch, total=3, resumed_titles=[])
        assert calls == [0, 1, 2], f"无可续跑来源时必须全量生成，实际 {calls}"
        titles = [n.get("title") for n in events[-1]["outline"]]
        assert titles == ["第1章", "第2章", "第3章"], titles


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(test_apply_library_same_title_nodes_does_not_400(None))
