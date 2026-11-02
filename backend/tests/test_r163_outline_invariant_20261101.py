"""R163（2026-11-01）遗留收口的**不变量锁**。

R162 翻转的核心承诺是「**放宽**」——旧 R151 合规四级树（L1 下 3~5 个 L2、
L2 下 3~5 个 L3、L3 下 4~6 个 L4）在新判据下仍应零违规，因此零数据迁移。
该承诺此前只写在报告与 `outline_rules` docstring 里，**没有测试锚**：任何
未来把 `LEAF_LEVEL` 改回 4、或把数量作用域扩回 L3 的改动，会让存量四级树
静默变红（生产 5 方案的 dry-run 重判结论随之失效）。

本文件把三条不变量锁成行为：
  1. 树判据：R151 合规四级树（4 L2 × 4 L3 × 4 L4）零违规；
  2. DB 行判据：同形态扁平行（点分编号子承父系）零违规；
  3. 生产形态：9 L1 × 3 L2 × 4 L3 三级树零违规（R162 放宽的直接受益面）。
"""
from __future__ import annotations

import json

from app.services.outline_rules import validate_outline_nodes, validate_section_rows


def node(title, children=None):
    return {"title": title, "description": "d",
            "children": (children if children is not None else [])}


def r151_four_level_tree(n_l2=4, n_l3=4, n_l4=4):
    """R151 时代的合规四级树（新判据下必须仍然合规）。"""
    return [node("第一章", [
        node(f"1.{j}", [
            node(f"1.{j}.{k}", [node(f"1.{j}.{k}.{m}") for m in range(1, n_l4 + 1)])
            for k in range(1, n_l3 + 1)
        ]) for j in range(1, n_l2 + 1)
    ])]


def four_level_rows(n_l2=4, n_l3=4, n_l4=4):
    """与上面同形态的 sections 扁平行（点分编号子承父系）。"""
    rows = [{"id": "a", "parent_id": "", "level": 1, "title": "第一章",
             "outline_json": json.dumps({"id": "1"})}]
    for j in range(1, n_l2 + 1):
        rows.append({"id": f"a{j}", "parent_id": "a", "level": 2,
                     "title": f"1.{j}",
                     "outline_json": json.dumps({"id": f"1.{j}"})})
        for k in range(1, n_l3 + 1):
            l3_id = f"a{j}j{k}"
            rows.append({"id": l3_id, "parent_id": f"a{j}", "level": 3,
                         "title": f"1.{j}.{k}",
                         "outline_json": json.dumps({"id": f"1.{j}.{k}"})})
            for m in range(1, n_l4 + 1):
                rows.append({"id": f"{l3_id}m{m}", "parent_id": l3_id, "level": 4,
                             "title": f"1.{j}.{k}.{m}",
                             "outline_json": json.dumps({"id": f"1.{j}.{k}.{m}"})})
    return rows


class TestR163ZeroMigrationInvariant:
    def test_r151_compliant_four_level_tree_still_compliant(self):
        """R162 放宽承诺：R151 合规四级树在新判据下零违规（零迁移前提）。"""
        assert validate_outline_nodes(r151_four_level_tree()) == []

    def test_r151_compliant_rows_still_compliant(self):
        """同一不变量在 DB 行口径（正文闸门实际入口）同样成立。"""
        assert validate_section_rows(four_level_rows()) == []

    def test_production_shape_three_level_tree_compliant(self):
        """生产形态量级（9 L1 × 3 L2 × 4 L3 = 144 节点）三级树零违规。"""
        tree = [node(f"第{i}章", [
            node(f"{i}.{j}", [node(f"{i}.{j}.{k}") for k in range(1, 5)])
            for j in range(1, 4)
        ]) for i in range(1, 10)]
        assert validate_outline_nodes(tree) == []
