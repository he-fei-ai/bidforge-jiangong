"""目录生成模块 · 潜在（生产不可达）结构行为的回归锁定（2026-10-08）

背景（承接本轮深度探查）
------------------------
生产目录树的来源只有两类：``json.loads`` 解析的 AI 结果、DB 行映射
（``_save_outline_to_db`` / ``_build_tree``）。二者**都不产生共享节点对象**，
且 ``normalize_outline`` 先把树裁剪到 3 级，因此下列异常结构在生产路径
**不可达**。但它们是 ``renumber_outline_nodes`` 环引用防护（``_path``）与
「就地改写、以对象身份编号」这一性能设计的边界属性，此前**从未被测**：

- ``test_perf_baseline_20260927.test_renumber_guards_against_cycles`` 只覆盖
  自环 + 60 级链不崩（且不校验编号），且其注释仍停留在旧的 ``_depth > 20``
  口径（2026-10-08 D-1 已把上限提到 ``_MAX_TREE_DEPTH=200``）；
- ``test_numbering_unification.test_depth_protection`` 覆盖「children 互引的
  环引用」；
- ``test_outline_generation_gaps_20261008``（D-1 / D-2）已完整锁定合法深嵌套
  的逐层编号与 ``level == 深度`` 不变量。

本文件只补齐**上述文件都未覆盖**的两项潜在契约，把「未测的潜在行为」升级为
「已测且刻意保留」，防止后续重构在无意识到下破坏这些鲁棒性保证。

**零源码改动**：全部断言锁定既有实现契约，不改变任何行为。
"""
from __future__ import annotations

from app.services.numbering import renumber_outline_nodes
from app.services.outline_utils import normalize_outline


def _collect_ids(nodes: list) -> list[dict]:
    """展平目录为节点列表（含深度），供整体不变量校验。"""
    out: list[dict] = []

    def _walk(ns, depth):
        for n in ns:
            if not isinstance(n, dict):
                continue
            out.append({"node": n, "depth": depth})
            _walk(n.get("children") or [], depth + 1)

    _walk(nodes, 1)
    return out


class TestDAGSharedObject:
    """跨分支共享节点对象（DAG，非环）：环防护不误判、始终有编号。

    ⚠️ 生产不可达（JSON/DB 不产共享引用）。本类锁定的是**刻意契约**：
    环引用防护按「当前递归路径」判定（``_path`` 进入前登记、退出后移除），
    因此同一对象挂在两个非祖先后代下不会被误判成环、不报错；同一对象
    被最后写入的父分支编号（last-writer）。若将来改为 first-writer 或
    去重，本类应随之**有意识地**更新 —— 这正是回归锁的意义。
    """

    def test_shared_leaf_across_sibling_roots_last_writer_no_crash(self):
        leaf = {"title": "共享叶子"}
        r1 = {"title": "R1", "children": [leaf]}
        r2 = {"title": "R2", "children": [leaf]}

        out = renumber_outline_nodes([r1, r2])

        # 两个根各自获得独立顶级编号
        assert [n["id"] for n in out] == ["1", "2"]
        # 两个根的 children[0] 指向同一对象（就地改写，无拷贝）
        assert out[0]["children"][0] is leaf
        assert out[1]["children"][0] is leaf
        # 共享对象的最终编号取最后写入分支（r2 → "2.1"），且始终非空
        assert leaf["id"] == "2.1"
        assert leaf["level"] == 2

    def test_shared_leaf_nested_at_two_depths_always_ided(self):
        # 同一对象既作直接子节点、又作孙节点：两处路径都不是环
        leaf = {"title": "X"}
        root = {"title": "R", "children": [leaf, {"title": "Y", "children": [leaf]}]}

        renumber_outline_nodes([root])

        # 下游按 node["id"] 取值不得 KeyError —— 共享节点始终有非空编号
        assert leaf.get("id")
        assert leaf["level"] == leaf["id"].count(".") + 1
        # 每个可达节点（含被共享者）都应有合法 id
        for rec in _collect_ids([root]):
            assert rec["node"].get("id"), "存在无编号节点"


class TestNormalizeOutputAliasFree:
    """normalize_outline 是环防护「树节点对象唯一」前提的守门人。

    从内联 dict 字面量（每个节点天然是独立对象）出发，normalize 产出的树
    不得引入任何共享别名，并剥离标题内嵌编号、令 level 与 id 段数一致。
    锁这条不变量 = 锁 renumber 的环防护前提不被 normalize 破坏。
    """

    def test_normalize_output_has_no_shared_node_objects(self):
        raw = [
            {"title": "1 甲", "children": [{"title": "1.1 乙"}, {"title": "1.2 丙"}]},
            {"title": "第二章 丁", "children": [{"title": "2.1 戊"}]},
        ]
        n = normalize_outline(raw)

        seen: set[int] = set()
        for rec in _collect_ids(n):
            oid = id(rec["node"])
            assert oid not in seen, "normalize 输出含共享节点对象别名"
            seen.add(oid)

        # 标题内嵌编号被剥离（防显示层双重编号）
        assert n[0]["title"] == "甲"
        assert n[1]["title"] == "丁"

        # level 与 id 段数在整棵树上一致
        for rec in _collect_ids(n):
            node = rec["node"]
            assert node["level"] == node["id"].count(".") + 1
