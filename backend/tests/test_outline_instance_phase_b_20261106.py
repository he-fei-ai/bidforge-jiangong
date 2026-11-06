"""Phase B（R179 · 2026-11-06）：模板实例化 + 节点双标签验收。

护栏目标
--------
1. 默认 ``build_outline`` 输出与现状逐字一致（节点仅三键）；
2. ``with_meta`` 后每个节点都带 ``content_type``（五类枚举）与 ``fact_deps``；
3. 只消费 ``is_resolved=1 且 has_conflict=0`` 的事实；未命中标 ``missing``；
4. 元数据经 ``preserve_node_meta`` 写入 ``sections.outline_json``，普通保存路径
   仍是三键（零差异）；
5. 开关默认关闭：路由 404。
"""

from __future__ import annotations

import json

import pytest
from app.config import settings
from app.routers.schemes import instantiate_scheme_outline
from app.routers.sections import _save_outline_to_db
from app.services import outline_templates as ot
from app.services.outline_instance import instantiate, instantiate_for_scheme
from fastapi import HTTPException


def _all_nodes(nodes):
    for n in nodes or []:
        if isinstance(n, dict):
            yield n
            yield from _all_nodes(n.get("children"))


async def _seed(db, pid="p1", sid="s1", name="深基坑支护专项施工方案"):
    await db.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "项目"))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, name))
    await db.commit()


async def _fact(db, fid, title, content, *, resolved=1, conflict=0, pid="p1", sid="s1", ref="地勘报告.pdf#p12"):
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, title, content, source_ref,"
        " is_resolved, has_conflict, confidence) VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, title, content, ref, resolved, conflict, 0.9),
    )
    await db.commit()


# ---------------------------------------------------------------- 默认输出


@pytest.mark.parametrize("key", sorted(ot.BUILDERS))
def test_default_build_outline_is_unchanged(key):
    """默认路径节点仅 title/description/children —— 与现状逐字一致。"""
    nodes = ot.BUILDERS[key]()
    for n in _all_nodes(nodes):
        assert set(n.keys()) == {"title", "description", "children"}, (key, n.title, set(n.keys()))


def test_with_meta_covers_every_node():
    """五类内容类型全覆盖 + 每节点都带 fact_deps 键。"""
    for key in sorted(ot.BUILDERS):
        nodes = ot.annotate_outline_meta(ot.BUILDERS[key]())
        for n in _all_nodes(nodes):
            assert n["content_type"] in ot.CONTENT_TYPES, (key, n["title"])
            assert isinstance(n["fact_deps"], list), (key, n["title"])
            if n["fact_deps"]:
                assert n.get("placeholder"), (key, n["title"])


def test_build_outline_with_meta_flag():
    plain = ot.build_outline("深基坑支护专项施工方案")
    meta = ot.build_outline("深基坑支护专项施工方案", with_meta=True)
    assert len(list(_all_nodes(plain))) == len(list(_all_nodes(meta)))
    assert all("content_type" in n for n in _all_nodes(meta))
    assert all("content_type" not in n for n in _all_nodes(plain))


# ---------------------------------------------------------------- 事实实例化


@pytest.mark.asyncio
async def test_instantiate_resolved_fact_shows_value_and_ref(db_conn):
    await _seed(db_conn)
    await _fact(db_conn, "f1", "周边环境", "东侧距既有建筑 8.5m，南侧为市政道路")
    res = await instantiate_for_scheme(db_conn, "s1", "p1")
    node = next(n for n in _all_nodes(res["tree"]) if "周边环境条件" == n.get("title"))
    assert node["fact_status"] == "resolved"
    assert node["fact_values"]["surroundings"]["value"] == "东侧距既有建筑 8.5m，南侧为市政道路"
    assert node["fact_source_ref"] == "地勘报告.pdf#p12"


@pytest.mark.asyncio
async def test_instantiate_missing_fact_marked_missing(db_conn):
    await _seed(db_conn)
    res = await instantiate_for_scheme(db_conn, "s1", "p1")
    node = next(n for n in _all_nodes(res["tree"]) if "工程地质与水文地质" == n.get("title"))
    assert node["fact_status"] == "missing"
    assert "ground_conditions" in node["missing_fact_deps"]
    assert "根据实际情况确定" not in json.dumps(res["tree"], ensure_ascii=False)
    assert res["stats"]["missing"] > 0


@pytest.mark.asyncio
async def test_instantiate_ignores_unresolved_and_conflict(db_conn):
    """未确认 / 有冲突事实一律不得填充（即便内容看起来完整）。"""
    await _seed(db_conn)
    await _fact(db_conn, "u1", "周边环境", "未确认值：距建筑 99m", resolved=0)
    await _fact(db_conn, "c1", "周边环境", "冲突值：距建筑 88m", conflict=1)
    res = await instantiate_for_scheme(db_conn, "s1", "p1")
    node = next(n for n in _all_nodes(res["tree"]) if "周边环境条件" == n.get("title"))
    assert node["fact_status"] == "missing"
    body = json.dumps(res["tree"], ensure_ascii=False)
    assert "99m" not in body and "88m" not in body


def test_instantiate_pure_function_stats():
    nodes = ot.build_outline("深基坑支护专项施工方案", with_meta=True)
    facts = [{"id": "x", "title": "周边环境", "content": "东侧 5m", "source_ref": "r.pdf", "confidence": 1.0}]
    tree, stats = instantiate(nodes, facts)
    assert stats["nodes"] == len(list(_all_nodes(nodes)))
    assert stats["resolved"] >= 1  # 多个节点都依赖「周边环境」，各自命中同一条事实
    assert stats["resolved"] + stats["partial"] + stats["missing"] + stats["no_dependency"] == stats["nodes"]
    assert nodes[0].get("fact_status") is None  # 深拷贝，不污染模板树


# ---------------------------------------------------------------- 落库与开关


@pytest.mark.asyncio
async def test_preserve_node_meta_writes_outline_json(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "outline_instantiate_enabled", True)
    await _seed(db_conn)
    await _fact(db_conn, "f1", "周边环境", "东侧距既有建筑 8.5m")
    res = await instantiate_scheme_outline("p1", "s1", dry_run=False, db=db_conn)
    assert res["dry_run"] is False

    cur = await db_conn.execute(
        "SELECT outline_json FROM sections WHERE scheme_id=? AND title=?", ("s1", "周边环境条件")
    )
    row = await cur.fetchone()
    payload = json.loads(row[0])
    assert payload["content_type"] == "experience"
    assert payload["fact_status"] == "resolved"
    assert payload["fact_source_ref"] == "地勘报告.pdf#p12"


@pytest.mark.asyncio
async def test_normal_save_keeps_three_keys(db_conn):
    """普通保存路径（preserve_node_meta 默认 False）仍写 id/level/confidence 三键。

    即便传入的是带元数据的树，默认路径也必须剥掉元数据 —— 这是「开关关闭时
    与既有 AI 目录生成输出零差异」的硬保证。
    """
    await _seed(db_conn)
    meta_tree = ot.build_outline("深基坑支护专项施工方案", with_meta=True)
    assert any("content_type" in n for n in _all_nodes(meta_tree))
    saved = await _save_outline_to_db(db_conn, "s1", meta_tree)
    assert saved["ok"] is True
    cur = await db_conn.execute("SELECT outline_json FROM sections WHERE scheme_id=?", ("s1",))
    rows = await cur.fetchall()
    assert rows
    for (raw,) in rows:
        assert set(json.loads(raw).keys()) == {"id", "level", "confidence"}


@pytest.mark.asyncio
async def test_instantiate_route_disabled_by_default(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "outline_instantiate_enabled", False)
    await _seed(db_conn)
    with pytest.raises(HTTPException) as e:
        await instantiate_scheme_outline("p1", "s1", dry_run=True, db=db_conn)
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_instantiate_dry_run_does_not_write_sections(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "outline_instantiate_enabled", True)
    await _seed(db_conn)
    res = await instantiate_scheme_outline("p1", "s1", dry_run=True, db=db_conn)
    assert res["dry_run"] is True and res["tree"]
    cur = await db_conn.execute("SELECT COUNT(*) FROM sections WHERE scheme_id=?", ("s1",))
    assert (await cur.fetchone())[0] == 0
