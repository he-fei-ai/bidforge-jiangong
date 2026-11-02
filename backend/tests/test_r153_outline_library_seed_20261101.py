# -*- coding: utf-8 -*-
"""R153（2026-11-01）目录库四级沉淀 + seed normalize 护栏。

两处修复（均为 R149 深度审计遗留项的现行缺陷化收口）：

  FIX-1  outline_library.create_from_scheme 的 build() 硬编码 level < 3 ——
         R151 四级标准（L4 为最小叶子）后，方案沉淀到目录库会静默丢弃全部
         L4 章节；孤儿行（parent 已删）同样无声消失。修复：下降深度改由
         OUTLINE_MAX_LEVEL 单一事实源驱动（保持有界下降 = 环防护语义），
         并以 dropped_sections 加法式键计数回传。

  FIX-2  seed_data._build_record 的 outline_json 直接 json.dumps，节点无
         id —— 前端 React key 取 outline_json.id 恒 undefined（R149 遗留
         L-10）。修复：统一过 normalize_outline_json（补 id/level + 点分
         编号），SEED_VERSION v3.0→v3.1 触发存量库刷新；normalize 失败
         回落原始序列化（预置内容绝不因规范化异常丢失）。

锁定不变量：
  A1 四级树全保留（修复前 L4 全丢 —— 本护栏的直接行为锚）
  A2 dropped_sections == 0（干净树零计数）
  A3 孤儿行计数回传且不落库
  A4 超出 OUTLINE_MAX_LEVEL 的脏行计数（L4 保留、L5 计数）
  A5 入库编号点分形态（normalize 重排）
  A6 word_budget 沉淀保持（2026-09-18 增强不回归）
  A7 定义处调用形态锚（R149 教训③：定义与使用都要锚；R144 教训②：
     完整赋值形态，防三元短路绕过与注释误伤）
  B1/B2 seed outline_json 全节点点分 id + 深度 ≤ OUTLINE_MAX_LEVEL
  B3 normalize 失败回落原始序列化
  B4 seed 端到端：库内预置条目带 id（版本 v3.1 由既有
     test_outline_library_full_update_20261001 锁定，此处不重复）
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid

import app.seed_data as seed_data
import pytest
from app.routers.outline_library import create_from_scheme
from app.seed_data import _build_record, _seed_outline_json
from app.services.outline_rules import OUTLINE_MAX_LEVEL

_DOTTED = re.compile(r"[0-9]+(\.[0-9]+)*")


# ---------------------------------------------------------------- 夹具

async def _mk_scheme(db, *, levels: str = "1234") -> str:
    """建 project + scheme + 指定层级链（levels 形如 "1234"=L1→L2→L3→L4）。"""
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    parent = ""
    for i, lv in enumerate(levels, start=1):
        rid = f"n{lv}"
        await db.execute(
            "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
            " description, level, sort_order, word_budget) VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, sid, pid, parent or None, f"第{lv}级", "d", int(lv), i,
             1200 if int(lv) == 3 else None))
        parent = rid
    await db.commit()
    return sid


async def _stored_outline(db, lib_id: str) -> list:
    cur = await db.execute(
        "SELECT outline_json FROM outline_library WHERE id=?", (lib_id,))
    row = await cur.fetchone()
    return json.loads(row["outline_json"])


def _flatten(nodes) -> list[dict]:
    out = []

    def walk(ns):
        for n in ns or []:
            out.append(n)
            walk(n.get("children"))
    walk(nodes)
    return out


def _depth(nodes) -> int:
    flat = _flatten(nodes)
    return max((n.get("level") or 0) for n in flat) if flat else 0


# ---------------------------------------------------------------- A 组

class TestCreateFromSchemeFourLevels:
    async def test_a1_four_level_tree_preserved(self, db_conn):
        """修复行为锚：L1~L4 全链沉淀，L4 不再被硬编码 level<3 丢弃。"""
        sid = await _mk_scheme(db_conn, levels="1234")
        rec = await create_from_scheme({"scheme_id": sid}, db=db_conn)
        lib_id = rec["id"]
        outline = await _stored_outline(db_conn, lib_id)
        flat = _flatten(outline)
        assert _depth(outline) == 4, f"L4 丢失：实际深度 {_depth(outline)}"
        assert len(flat) == 4
        assert any(n["title"] == "第4级" for n in flat)

    async def test_a2_clean_tree_dropped_zero(self, db_conn):
        sid = await _mk_scheme(db_conn, levels="1234")
        rec = await create_from_scheme({"scheme_id": sid}, db=db_conn)
        assert rec["dropped_sections"] == 0

    async def test_a3_orphan_counted_not_persisted(self, db_conn):
        """孤儿行（parent_id 悬空）：计数回传、不进树。"""
        sid = await _mk_scheme(db_conn, levels="1234")
        await db_conn.execute(
            "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
            " description, level, sort_order) VALUES(?,?,?,?,?,?,?,?)",
            ("orph", sid, "p-unknown", "ghost-parent", "孤儿章节", "d", 2, 99))
        await db_conn.commit()
        rec = await create_from_scheme({"scheme_id": sid}, db=db_conn)
        assert rec["dropped_sections"] == 1
        flat_titles = [n["title"] for n in _flatten(await _stored_outline(
            db_conn, rec["id"]))]
        assert "孤儿章节" not in flat_titles

    async def test_a4_over_deep_dirty_row_counted(self, db_conn):
        """L5 脏行（直写 DB 越过落库 normalize）：L4 保留、L5 计数回传。"""
        sid = await _mk_scheme(db_conn, levels="12345")
        rec = await create_from_scheme({"scheme_id": sid}, db=db_conn)
        assert rec["dropped_sections"] == 1, "L5 脏行应被计数"
        outline = await _stored_outline(db_conn, rec["id"])
        assert _depth(outline) == 4, "L4 必须保留为最小叶子"
        assert _depth(outline) <= OUTLINE_MAX_LEVEL

    async def test_a5_dotted_ids_after_normalize(self, db_conn):
        sid = await _mk_scheme(db_conn, levels="1234")
        rec = await create_from_scheme({"scheme_id": sid}, db=db_conn)
        for n in _flatten(await _stored_outline(db_conn, rec["id"])):
            assert n.get("id") and _DOTTED.fullmatch(str(n["id"])), \
                f"节点缺点分编号: {n.get('title')} id={n.get('id')}"

    async def test_a6_word_budget_survives(self, db_conn):
        """2026-09-18 增强不回归：L3 的 word_budget 随沉淀保留。"""
        sid = await _mk_scheme(db_conn, levels="1234")
        rec = await create_from_scheme({"scheme_id": sid}, db=db_conn)
        flat = _flatten(await _stored_outline(db_conn, rec["id"]))
        wb = {n["title"]: n.get("word_budget") for n in flat}
        assert wb["第3级"] == 1200, f"L3 word_budget 丢失: {wb}"

    def test_a7_definition_shape_anchor(self):
        """定义处调用形态锚（R149 教训③ + R144 教训②）：

        - 正向：完整赋值形态 ``if level < OUTLINE_MAX_LEVEL else []`` 必须在
          create_from_scheme 源内（防回退成字面 3 / 防三元短路绕过）；
        - 负向：``if level < 3 else`` 完整形态不得出现（注释中记载的旧形态
          无 else 尾巴，不会被误伤）。
        """
        src = inspect.getsource(create_from_scheme)
        assert "if level < OUTLINE_MAX_LEVEL else []" in src
        assert "if level < 3 else" not in src


# ---------------------------------------------------------------- B 组

class TestSeedOutlineNormalized:
    def test_b1_seed_records_have_dotted_ids(self):
        rec = _build_record("基坑工程专项施工方案", "基坑与土方")
        outline = json.loads(rec["outline_json"])
        flat = _flatten(outline)
        assert flat, "seed 目录不应为空"
        ids = [str(n.get("id") or "") for n in flat]
        assert all(ids), "存在无 id 节点（React key undefined 缺陷回归）"
        assert all(_DOTTED.fullmatch(i) for i in ids)
        assert len(set(ids)) == len(ids), "seed 节点 id 重复"

    def test_b2_seed_depth_within_standard(self):
        rec = _build_record("基坑工程专项施工方案", "基坑与土方")
        outline = json.loads(rec["outline_json"])
        assert _depth(outline) <= OUTLINE_MAX_LEVEL

    def test_b3_normalize_failure_falls_back_to_raw(self, monkeypatch):
        """normalize 失败（default 空）回落原始序列化——预置内容不因规范化
        异常而丢失，最坏维持旧行为。"""
        raw_shape = json.dumps(
            [{"title": "t", "description": "", "children": []}],
            ensure_ascii=False)
        for bad in ("", "[]"):
            monkeypatch.setattr(
                seed_data, "normalize_outline_json", lambda *a, _b=bad, **k: _b)
            out = _seed_outline_json(
                [{"title": "t", "description": "", "children": []}])
            assert out == raw_shape, f"default={bad!r} 未回落原始序列化"
        monkeypatch.undo()

    def test_b4_build_record_uses_seed_helper(self):
        """源码形态锚：_build_record 的 outline_json 必须经 _seed_outline_json。"""
        src = inspect.getsource(_build_record)
        assert '"outline_json": _seed_outline_json(outline)' in src


# ---------------------------------------------------------------- 直跑入口

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
