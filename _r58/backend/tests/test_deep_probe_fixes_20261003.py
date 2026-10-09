"""目录生成 + 全局事实模块 · 深度探查修复回归护栏（2026-10-03）。

锁定本轮三项已确认缺陷修复：
- B1（跨模块传递丢失）：generate_content 入口解析的 force_full_repair
  必须透传给收尾一致性修复 repair_agent.run_repair —— 旧实现解析后从未
  消费（ruff F841 坐实死变量），前端「强制全量重修」在主生成链路静默失效；
  独立修复端点 consistency_repair.py 消费正常，两条路径口径分叉。
- B2（作用域不对称）：update_fact 分组重建的旧行查询必须按命中行的
  project/scheme 限定（与 delete_fact 同口径）—— group_id 不是全局唯一
  约束，历史/旧版复制方案残留的跨方案重复 group_id 会让 grow 取到别的
  作用域的行，重建插错作用域、越域删除。
- B4（静默丢章）：_save_outline_to_db 必须拒绝重复的 __original_id ——
  同一 DB 主键被两个节点携带时旧实现对同一行发两次 UPDATE，两节点坍缩
  成一章、子章节 parent 错挂，返回 ok 却目录树少一章。
"""
import ast
import inspect
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import app.routers.sections as sections_mod
import pytest
from app.db import get_conn, init_db
from app.models import FactGroupUpdate
from app.routers.sse_handlers import generate_content
from fastapi import HTTPException


@pytest.fixture
async def db_ctx(tmp_path):
    """一套临时库 + 两个互不相干的 (project, scheme) 作用域。"""
    _appdb.DB_PATH = tmp_path / "deep-probe-20261003.sqlite"
    await init_db()
    db = await get_conn()
    scopes = []
    for _ in range(2):
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
        scopes.append((pid, sid))
    await db.commit()
    yield db, scopes
    await db.execute("DELETE FROM global_facts")
    await db.execute("DELETE FROM sections")
    await db.execute("DELETE FROM schemes")
    await db.execute("DELETE FROM projects")
    await db.commit()


# ---------------------------------------------------------------------------
# B1 · force_full_repair 透传接线（静态锁：判据指向真实的 run_repair 调用）
# ---------------------------------------------------------------------------

def _run_repair_call_keywords():
    """AST 扫描 generate_content（含嵌套 _run_consistency_pipeline），
    返回所有 `run_repair(...)` 调用的关键字名集合列表。

    只取 Call 节点本身的关键字，不匹配纯文本 —— 注释/字符串里的同名字样
    不算接线（§5.7 判据要指向真正的调用点）。
    """
    src = inspect.getsource(generate_content)
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (getattr(fn, "attr", "") or getattr(fn, "id", ""))
        if name == "run_repair":
            found.append({kw.arg for kw in node.keywords})
    return found


def test_generate_content_wires_force_full_repair_into_run_repair():
    """generate_content 的 run_repair 调用必须携带 force_full_repair=。"""
    calls = _run_repair_call_keywords()
    assert calls, "generate_content 内应存在 run_repair 调用"
    assert any("force_full_repair" in kws for kws in calls), (
        "force_full_repair 必须透传给 run_repair：入口解析却不消费 = 前端"
        "「强制全量重修」勾选在正文生成链路静默失效（2026-10-03 B1 回归）")


def test_force_full_repair_value_is_the_parsed_entry_flag():
    """透传的值必须是入口 _coerce_bool 解析出的同名变量（不是重新硬编码）。"""
    src = inspect.getsource(generate_content)
    tree = ast.parse(src)
    hit = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and (getattr(node.func, "attr", "") or getattr(node.func, "id", "")) == "run_repair"):
            for kw in node.keywords:
                if kw.arg == "force_full_repair":
                    assert isinstance(kw.value, ast.Name) and kw.value.id == "force_full_repair", (
                        "run_repair(force_full_repair=...) 必须引用入口解析的同名变量，"
                        "禁止改回常量 True/False 造成开关形同虚设")
                    hit = True
    assert hit


def test_force_full_repair_still_parsed_at_entry():
    """入口解析不得被摘掉（防止「没人消费就删解析」把前端契约也断了）。"""
    src = inspect.getsource(generate_content)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "_coerce_bool"):
            for arg in node.args:
                if (isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "get"
                        and len(arg.args) == 1
                        and arg.args[0].value == "force_full_repair"):
                    return
    pytest.fail("generate_content 入口必须保留 body.get(\"force_full_repair\") 的布尔归一解析")


# ---------------------------------------------------------------------------
# B2 · update_fact 分组重建作用域限定
# ---------------------------------------------------------------------------

async def _seed_fact(db, pid, sid, gid, fid, name, value):
    content = gf._build_fact_content(name, value, False)
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, gid, "工期安排", name, content, "schedule",
         '[{"file":"招标文件","quote":"x"}]', 0, 0.9, 1, 0, "", name))
    await db.commit()


async def _rows_of(db, pid, sid):
    cur = await db.execute(
        "SELECT id, title, content FROM global_facts "
        "WHERE project_id=? AND COALESCE(scheme_id,'')=?", (pid, sid))
    return [dict(r) for r in await cur.fetchall()]


async def test_update_fact_group_rebuild_does_not_touch_foreign_scope(db_ctx):
    """跨方案重复 group_id（历史残留形态）：编辑方案1的分组绝不触碰方案2。"""
    db, ((pid1, sid1), (pid2, sid2)) = db_ctx
    dup_gid = "group-dup-legacy"
    # 方案1：分组两行；方案2：同 group_id 一行（旧版复制方案残留形态）
    await _seed_fact(db, pid1, sid1, dup_gid, "f1a", "总工期", "30天")
    await _seed_fact(db, pid1, sid1, dup_gid, "f1b", "开工日期", "2026-01-01")
    await _seed_fact(db, pid2, sid2, dup_gid, "f2a", "别的方案的工期", "99天")

    data = FactGroupUpdate(id="f1a", title="工期安排",
                           content="- 总工期: 45天\n- 开工日期: 2026-02-01")
    resp = await gf.update_fact("f1a", data, scheme_id=sid1, db=db)
    assert resp["ok"] is True

    # 方案1：分组按新内容重建（2 行，值已更新）
    rows1 = await _rows_of(db, pid1, sid1)
    by_name = {r["title"]: r["content"] for r in rows1}
    assert by_name.get("总工期") and "45天" in by_name["总工期"]
    assert by_name.get("开工日期") and "2026-02-01" in by_name["开工日期"]
    # 旧行必须被本作用域的替换取代，不得残留
    assert sum(1 for r in rows1 if r["title"] == "总工期") == 1

    # 方案2：那一行**原样不动**（旧实现 grow=old_rows[0] 可能取到它并按
    # 其作用域 DELETE+INSERT，把它卷进重建甚至删除）
    rows2 = await _rows_of(db, pid2, sid2)
    assert len(rows2) == 1 and rows2[0]["id"] == "f2a"
    assert "99天" in rows2[0]["content"]


async def test_update_fact_group_rebuild_normal_case_unchanged(db_ctx):
    """group_id 唯一（现代数据）：作用域限定不改变既有行为（向后兼容）。"""
    db, ((pid1, sid1), _) = db_ctx
    gid = uuid.uuid4().hex
    await _seed_fact(db, pid1, sid1, gid, "fa", "总工期", "30天")
    await _seed_fact(db, pid1, sid1, gid, "fb", "基坑深度", "8.5m")

    data = FactGroupUpdate(id="fa", title="工期安排",
                           content="- 总工期: 60天\n- 基坑深度: 8.5m")
    resp = await gf.update_fact("fa", data, scheme_id=sid1, db=db)
    assert resp["ok"] is True
    rows = await _rows_of(db, pid1, sid1)
    assert len(rows) == 2
    vals = {r["title"]: r["content"] for r in rows}
    assert "60天" in vals["总工期"]
    assert "8.5m" in vals["基坑深度"]


# ---------------------------------------------------------------------------
# B4 · _save_outline_to_db 拒绝重复 __original_id
# ---------------------------------------------------------------------------

async def _first_save(db, sid, pid):
    """落一颗两章的目录树，返回两个真实 DB 主键。"""
    outline = [{"id": "local_a", "title": "工程概况", "children": []},
               {"id": "local_b", "title": "施工部署", "children": []}]
    resp = await sections_mod._save_outline_to_db(db, sid, outline, source="ai")
    assert resp["ok"] and resp["count"] == 2
    ids = [t["id"] for t in resp["tree"]]
    assert len(ids) == 2
    return ids


async def test_save_outline_rejects_duplicate_original_id(db_ctx):
    """两个节点携带同一 __original_id → 400 显式拒绝（而非静默坍缩一章）。"""
    db, ((pid, sid), _) = db_ctx
    id_a, _id_b = await _first_save(db, sid, pid)

    dup = [{"id": "x1", "title": "第一章改名", "__original_id": id_a, "children": []},
           {"id": "x2", "title": "第二章冒充", "__original_id": id_a, "children": []}]
    with pytest.raises(HTTPException) as exc:
        await sections_mod._save_outline_to_db(db, sid, dup, source="手动编辑")
    assert exc.value.status_code == 400
    assert "重复" in str(exc.value.detail)

    # 拒绝发生在任何写入之前：库内结构原样保留（不是改了一半）
    cur = await db.execute(
        "SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())[0] == 2
    cur = await db.execute(
        "SELECT title FROM sections WHERE id=?", (id_a,))
    assert (await cur.fetchone())["title"] == "工程概况"


async def test_save_outline_duplicate_child_id_also_rejected(db_ctx):
    """重复主键藏在子章节层同样拒绝（防御覆盖全深度）。"""
    db, ((pid, sid), _) = db_ctx
    id_a, id_b = await _first_save(db, sid, pid)
    dup = [{"id": "n1", "title": "工程概况", "__original_id": id_a,
            "children": [{"id": "n11", "title": "伪装子节",
                          "__original_id": id_b, "children": []}]},
           {"id": "n2", "title": "施工部署", "__original_id": id_b,
            "children": []}]
    with pytest.raises(HTTPException) as exc:
        await sections_mod._save_outline_to_db(db, sid, dup, source="手动编辑")
    assert exc.value.status_code == 400


async def test_save_outline_normal_roundtrip_still_preserves_sections(db_ctx):
    """正常回传（各节点独立 __original_id）：更新而非重建，零影响面。"""
    db, ((pid, sid), _) = db_ctx
    id_a, id_b = await _first_save(db, sid, pid)
    outline = [{"id": "1", "title": "工程概况（改）", "__original_id": id_a,
                "children": []},
               {"id": "2", "title": "施工部署", "__original_id": id_b,
                "children": []}]
    resp = await sections_mod._save_outline_to_db(db, sid, outline, source="手动编辑")
    assert resp["ok"] and resp["count"] == 2
    cur = await db.execute(
        "SELECT title FROM sections WHERE id=?", (id_a,))
    assert (await cur.fetchone())["title"] == "工程概况（改）"
