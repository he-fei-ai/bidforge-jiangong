"""R179（2026-11-05）全局事实模块深度审计 · 修复回归。

覆盖本轮新增修复：
1. ``_validate_adjust_ops``：同一 AI 计划内重复 ``add`` 必须被拦下
   （旧实现只比对既有键，计划内两条同名 add 会全部放行 → 双值注入）。
2. ``create_fact`` 手工新增同名守卫：
   - 同 key 同值且既有行**可注入** → 幂等去重（不新增行，返回既有分组）；
   - 同 key 同值但既有行未过门控（模拟/待确认/过期/矛盾）→ 409（R183 口径，
     旧实现静默去重会让补录的真实值永远进不了正文）；
   - 同 key 不同值 → 409，且一行都不落库；
   - 方案级新增必须能看见项目共享同名事实；
   - 项目级新增不得误伤其他方案的私有同名事实。
3. 附录「项目关键事实」第二列剥离与第一列重复的名称前缀。

测试直接调用路由函数（与既有测试风格一致），避免 TestClient 跨事件循环
持有 aiosqlite 连接导致的偶发失败。
"""

import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import pytest
from app.db import get_conn, init_db
from app.models import FactGroupIn, FactItem
from fastapi import HTTPException


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "facts-r179.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.close()


def _group(title: str, items: list[FactItem], category: str = "personnel") -> FactGroupIn:
    return FactGroupIn(title=title, content="", category=category, items=items)


async def _rows(db, sid: str) -> list[dict]:
    cur = await db.execute(
        "SELECT id, group_id, title, content, fact_key, is_simulated, is_resolved "
        "FROM global_facts WHERE scheme_id=? ORDER BY title, id",
        (sid,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------- 1. AI 调整计划


def test_r179_validate_adjust_ops_drops_duplicate_add_within_plan():
    """同一计划内两条同名 add：第二条必须被丢弃并登记 duplicate_fact_key。

    变异验证：删掉 ``existing_keys.add(ak)`` 后本用例必红（旧行为返回 2 条）。
    """
    ignored: list[dict] = []
    ops, _summary = gf._validate_adjust_ops(
        {
            "operations": [
                {"op": "add", "name": "项目经理", "value": "张三", "category": "personnel"},
                {"op": "add", "name": "项目经理", "value": "李四", "category": "personnel"},
            ]
        },
        set(),
        existing_keys=set(),
        ignored_out=ignored,
    )
    assert len(ops) == 1
    assert ops[0]["value"] == "张三"
    assert [i["reason"] for i in ignored] == ["duplicate_fact_key"]
    # 同名同值的第三条同样被拦（键索引已写入）
    ignored2: list[dict] = []
    ops2, _ = gf._validate_adjust_ops(
        {
            "operations": [
                {"op": "add", "name": "项目经理", "value": "张三"},
                {"op": "add", "name": "项目经理", "value": "张三"},
            ]
        },
        set(),
        existing_keys=set(),
        ignored_out=ignored2,
    )
    assert len(ops2) == 1
    assert ignored2[0]["reason"] == "duplicate_fact_key"


# ------------------------------------------------------- 2. 手工新增同名守卫


async def test_r179_create_fact_same_key_different_value_rejected(ctx):
    db, _pid, sid = ctx
    await gf.create_fact(
        _group("人员", [FactItem(name="项目经理", value="张三")]),
        scheme_id=sid,
        project_id="",
        db=db,
    )
    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(
            _group("人员", [FactItem(name="项目经理", value="李四")]),
            scheme_id=sid,
            project_id="",
            db=db,
        )
    assert ei.value.status_code == 409
    assert "项目经理" in ei.value.detail
    assert "张三" in ei.value.detail
    rows = await _rows(db, sid)
    assert len(rows) == 1 and rows[0]["title"] == "项目经理"


async def test_r179_create_fact_same_key_same_value_is_idempotent(ctx):
    db, _pid, sid = ctx
    first = await gf.create_fact(
        _group("人员", [FactItem(name="项目经理", value="张三")]),
        scheme_id=sid,
        project_id="",
        db=db,
    )
    res = await gf.create_fact(
        _group("人员", [FactItem(name="项目经理", value="张三")]),
        scheme_id=sid,
        project_id="",
        db=db,
    )
    rows = await _rows(db, sid)
    assert len(rows) == 1
    assert res["ok"] is True
    assert res["deduplicated"] == 1
    assert res["id"] == first["id"] == rows[0]["group_id"]


async def test_r179_create_fact_duplicate_within_same_payload(ctx):
    db, _pid, sid = ctx
    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(
            _group(
                "人员",
                [FactItem(name="项目经理", value="张三"), FactItem(name="项目经理", value="李四")],
            ),
            scheme_id=sid,
            project_id="",
            db=db,
        )
    assert ei.value.status_code == 409
    assert await _rows(db, sid) == []  # 整组回滚，零脏数据

    res = await gf.create_fact(
        _group(
            "人员",
            [FactItem(name="项目经理", value="张三"), FactItem(name="项目经理", value="张三")],
        ),
        scheme_id=sid,
        project_id="",
        db=db,
    )
    rows = await _rows(db, sid)
    assert len(rows) == 1
    assert res["deduplicated"] == 1


async def test_r179_create_fact_scope_shared_and_private(ctx):
    """方案级新增看见项目共享；项目级新增不误伤其他方案私有事实。"""
    db, pid, sid = ctx
    # 项目共享事实（scheme_id 为空）
    await gf.create_fact(
        _group("人员", [FactItem(name="项目经理", value="张三")]),
        scheme_id="",
        project_id=pid,
        db=db,
    )
    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(
            _group("人员", [FactItem(name="项目经理", value="李四")]),
            scheme_id=sid,
            project_id="",
            db=db,
        )
    assert ei.value.status_code == 409

    # 另一个方案私有同名事实：项目级新增不应被它拦截
    other_sid = uuid.uuid4().hex
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (other_sid, pid, "s2"))
    await db.commit()
    await gf.create_fact(
        _group("人员", [FactItem(name="安全员", value="王五")]),
        scheme_id=other_sid,
        project_id="",
        db=db,
    )
    res = await gf.create_fact(
        _group("人员", [FactItem(name="安全员", value="赵六")]),
        scheme_id="",
        project_id=pid,
        db=db,
    )
    assert res["ok"] is True
    assert res["deduplicated"] == 0


async def test_r179_create_fact_simulated_marker_same_value_now_blocked(ctx):
    """同名同值但既有行是模拟值时，不再静默去重：409 引导先确认既有行（R183 口径）。

    旧断言（`deduplicated == 1` + 仅 1 行）只校验行数，未校验可注入性，导致：
    模拟值行原样保留（`is_simulated=1`），被 `_FACTS_INJECT_WHERE` 拦截，
    用户补录的真实值从未进入正文 —— 接口回 ok、功能静默为空、测试一直绿。
    R183 起宁可 409 也不谎报成功；同值也不再吞掉真实的「转正」意图。
    """
    db, _pid, sid = ctx
    await gf.create_fact(
        _group(
            "技术参数",
            [FactItem(name="基坑深度", value="12.5m", is_simulated=True, is_resolved=False)],
            category="tech_param",
        ),
        scheme_id=sid,
        project_id="",
        db=db,
    )
    with pytest.raises(HTTPException) as ei:
        await gf.create_fact(
            _group("技术参数", [FactItem(name="基坑深度", value="12.5m")], category="tech_param"),
            scheme_id=sid,
            project_id="",
            db=db,
        )
    assert ei.value.status_code == 409
    detail = str(ei.value.detail)
    assert "未通过注入门控" in detail
    assert "12.5m" in detail
    rows = await _rows(db, sid)
    assert len(rows) == 1
    assert rows[0]["is_simulated"] == 1


# ------------------------------------------------------------ 3. 附录名称去重


def test_r179_appendix_strip_leading_fact_name():
    # 必须先导入 export（facade 先加载 parts），否则直接 import parts 会触发
    # parts ↔ export 的半初始化循环导入。
    import app.routers.export  # noqa: F401
    from app.routers._export_docx_parts import _strip_leading_fact_name

    assert _strip_leading_fact_name("**基坑深度**: 12.5m", "基坑深度") == "12.5m"
    assert _strip_leading_fact_name("基坑深度：12.5m", "基坑深度") == "12.5m"
    assert _strip_leading_fact_name("12.5m", "基坑深度") == "12.5m"
    # 名称含正则元字符也按字面匹配
    assert _strip_leading_fact_name("**C30(混凝土)**: 坍落度 180mm", "C30(混凝土)") == "坍落度 180mm"
    # 名称不是行首时原样保留，避免误删取值内容
    assert _strip_leading_fact_name("采用基坑深度 12.5m", "基坑深度") == "采用基坑深度 12.5m"


def test_r179_appendix_cell_paragraph_no_duplicate_title():
    """成稿单元格只保留取值，第一列名称不重复出现在第二列。"""
    import app.routers.export  # noqa: F401 — 先加载 facade，避免循环导入
    from app.routers._export_docx_parts import _appendix_cell_paragraph
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    cell = table.rows[0].cells[1]
    _appendix_cell_paragraph(cell, "- **基坑深度**: 12.5m", "宋体", first=True, strip_name="基坑深度")
    assert cell.text.strip() == "12.5m"
    # 兼容：不传 strip_name 时保持旧行为（名称保留，仅剥列表符）
    doc2 = Document()
    table2 = doc2.add_table(rows=1, cols=2)
    cell2 = table2.rows[0].cells[1]
    _appendix_cell_paragraph(cell2, "- **基坑深度**: 12.5m", "宋体", first=True)
    assert "基坑深度" in cell2.text and "12.5m" in cell2.text
