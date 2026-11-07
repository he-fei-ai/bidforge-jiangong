"""_build_tree 自父/环拓扑的健壮性与编号口径对齐（R184，2026-11-07）。

实测先在（证伪结论，见本轮报告）：
- 自父 ``parent_id == id``：旧 ``_build_tree`` 把该节点挂进自己的 children，
  于是它不再是根，``_sort_children`` 从 roots 出发**遍历不到** → 实测**不抛
  RecursionError**；真实后果是该节点及其子节点从目录树中**静默消失**。而
  ``content_utils.recompute_section_numbers`` 的扁平→嵌套适配层有 ``pid != sid``
  守卫，把自父节点当根 → 编号 ``1``。两侧分叉的实态是「丢节点 vs 当根」，
  不是 500。
- 两点环 A→B→A、三点环 A→B→C→A：同一机制下环内节点都不是根，roots 为空，
  ``_sort_children`` 不进入环 → 实测不递归、不抛错，环内组件整体不出现；
  适配层同样返回空编号，两侧一致。

本文件锁死：自父按根处理（与适配层同口径），环不递归且不吞掉正常根。
"""

import json

from app.routers.sections import _build_tree
from app.services.content_utils import recompute_section_numbers
from app.services.numbering import renumber_section_outline_ids


async def _seed(db, scheme_id: str, rows: list[tuple]) -> None:
    """rows = (id, parent_id, title, sort_order, level)"""
    for nid, pid, title, sort_order, level in rows:
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
            " sort_order, status, word_budget) VALUES (?,?,?,?,?,?,?,?,?)",
            (nid, scheme_id, "p", pid, title, level, sort_order, "empty", 1500),
        )
    await db.commit()


def _rows_to_flat(rows: list[tuple]) -> list[dict]:
    return [
        {
            "id": nid,
            "parent_id": pid,
            "title": title,
            "sort_order": sort_order,
            "level": level,
            "outline_json": "",
        }
        for nid, pid, title, sort_order, level in rows
    ]


def _tree_number_map(roots: list) -> dict:
    numbers = {}
    for payload, _level, sid in renumber_section_outline_ids(roots):
        num = str(json.loads(payload).get("id") or "")
        if num:
            numbers[sid] = num
    return numbers


async def test_self_parent_treated_as_root_and_parity(db_conn):
    """自父节点按根处理：不再静默消失，编号与 content_utils 适配层一致。"""
    rows = [
        ("S", "S", "自父章节", 0, 1),
        ("C", "S", "子章节", 1, 2),
    ]
    await _seed(db_conn, "sc_r184_self", rows)

    roots = await _build_tree(db_conn, "sc_r184_self")
    assert [r["id"] for r in roots] == ["S"]
    assert [c["id"] for c in roots[0]["children"]] == ["C"]
    assert _tree_number_map(roots) == {"S": "1", "C": "1.1"}
    assert recompute_section_numbers(_rows_to_flat(rows)) == _tree_number_map(roots)


async def test_two_node_cycle_does_not_recurse_and_component_dropped(db_conn):
    """A→B→A：不抛 RecursionError，环内组件不出现，与适配层一致（空编号）。"""
    rows = [
        ("A", "B", "环A", 0, 1),
        ("B", "A", "环B", 1, 1),
    ]
    await _seed(db_conn, "sc_r184_two", rows)

    roots = await _build_tree(db_conn, "sc_r184_two")
    assert roots == []
    assert recompute_section_numbers(_rows_to_flat(rows)) == {}


async def test_three_node_cycle_keeps_normal_root(db_conn):
    """A→B→C→A：不递归，环内组件丢弃，正常根不受影响；两侧编号一致。"""
    rows = [
        ("R", "", "正常根", 0, 1),
        ("A", "B", "环A", 1, 1),
        ("B", "C", "环B", 2, 1),
        ("C", "A", "环C", 3, 1),
    ]
    await _seed(db_conn, "sc_r184_three", rows)

    roots = await _build_tree(db_conn, "sc_r184_three")
    assert [r["id"] for r in roots] == ["R"]
    assert _tree_number_map(roots) == {"R": "1"}
    assert recompute_section_numbers(_rows_to_flat(rows)) == {"R": "1"}
