"""目录生成模块 · 编号命名空间 / 分步生成状态 / 跨模块传递 缺口对抗测试（2026-10-08）

本文件只补齐**未被既有回归测试覆盖**的目录生成契约，不重复已有用例。
全部断言的 API 均来自仓库既有通过的测试文件（事实源）：

- ``tests/test_numbering_unification.py``        → ``app.services.numbering`` 公开面
  （renumber_outline_nodes / renumber_section_outline_ids / stored_id_to_* /
  renumber_section_body_subheadings / CHINESE_NUMBERS / ALPHABET）
- ``tests/test_numbering_batch_perf_20260927.py``→ validate_scheme_numbering_consistency /
  normalize_section_content_subheadings / load_scheme_section_index
- ``tests/test_content_generation_g12.py``        → ``app.services.ai.task_registry``
  （_tasks / finish_task / get_conn / update_progress）与
  ``app.routers.sse_handlers._attach_checkpoint_result``、outline_generation 任务类型
- ``tests/test_export_atomicity_r1_20261005.py``  → ``app.routers.export`` 静态断言风格

缺口编号与本专项报告 §五 一一对应：

- **D-1** 目录树深度：既有测试只覆盖「循环引用」的深度防护，**合法深嵌套**
  （长方案常见 3~6 级，个别专项达 8 级）从未被验证；纯递归实现会直接
  RecursionError，且编号不会重算。
- **D-2** 多级编号同步：既有 §3 只断言了 1 层子节点的 level 同步，
  4~5 级树下「level == 编号点分深度」不变量从未被整体校验。
- **D-3** 标题修改后编号未重算：既有 §3 从根重排，但**中间插入 / 删除节点导致的
  级联平移**（新增第一节 → 原有全部 +1）无覆盖，这正是用户可见症状。
- **D-4** 分步生成状态错乱（终态内存泄漏）：G12-5 只把守卫建在
  ``task_type="content_generation"`` 上；目录生成的 ``outline_generation`` 任务
  走同一个 ``finish_task``，写库失败后的 ``_tasks`` 残留路径无守卫。
- **D-5** 分步生成进度落库入口缺失：``app/db.py`` 头部注释记录的真实生产事故
  「task_registry.update_progress 落库失败 ×4、保存目录生成 checkpoint 失败、
  finish_task 终态落库失败: database is locked」指向同一入口，是否存在/幂等无锚点。
- **D-6** 跨模块数据传递丢失（正向路径）：G12 只测了 ``_attach_checkpoint_result``
  的**跳过**分支，目录生成 checkpoint 的 outline_result 被正文生成**正常消费**的
  正向路径从未被验证 —— 静默丢弃只表现为「正文生成读不到目录」，无异常无告警。
- **D-7** 编号命名空间冲突：既有 §11 测了 rel0/rel1/rel2 降级，
  但「正文子标题永不产出 X.Y 点分编号」这一命名空间隔离的**全局**不变量
  （rel 1..6 全深度）未被整体断言。
- **D-8** 空数据 / 超长内容 / 编码：空树、全空标题、BOM、CRLF、全角数字标题、
  单章超大正文的鲁棒性均无覆盖。

运行方式（在源码可读的环境）：
    cd backend && python -m pytest tests/test_outline_generation_gaps_20261008.py -q

本文件不引入任何新配置项，也不修改任何既有源码；仅锁定行为契约。
"""
from __future__ import annotations

import inspect
import json
import sys

import pytest


# ---------------------------------------------------------------------------
# 导入守卫：``app`` 包在本仓库受 ACL 保护的环境中不可导入
# （实测 ``PermissionError: [Errno 13] app/__init__.py``）。
# 这里把「无法导入」转成显式 skip，而非 collection 期 ImportError 让整仓
# 测试中断；skip 原因本身即为可操作结论。
# ---------------------------------------------------------------------------
def _app_available() -> bool:
    try:
        import app.services.numbering  # noqa: F401
        return True
    except BaseException:
        return False


_REQUIRES_APP = pytest.mark.skipif(
    not _app_available(),
    reason="本环境无法导入 app（ACL 只读保护）；请在源码可读的环境执行本文件。",
)


# ============================================================
# D-1 / D-2 / D-8：目录树构建与多级编号同步
# ============================================================
@_REQUIRES_APP
class TestOutlineTreeDepthAndSync:
    """目录树构建的鲁棒性：深度、层级同步、字段保全、空数据。"""

    def test_deep_legit_tree_does_not_recurse_over_limit(self):
        """D-1：合法深嵌套必须完成编号且 level 与深度严格对应。

        既有 test_numbering_unification.test_depth_protection 只喂了
        「children 互相引用的循环引用」。真实长方案的深嵌套是**合法链**，
        走的是同一条递归路径 —— 若实现无显式深度上限，深层节点会直接
        RecursionError，导致整份目录生成 500 且无任何错误定位信息。

        ✅ 用例修正（2026-10-08）：原遍历 DEPTH+1 轮且**每轮**都取
        ``children[0]`` —— 最后一轮取的是末端节点（children 为空）→ IndexError，
        把「实现截断」误报成「用例崩」；且原断言期望 ``["1","2","3",...]``
        是**兄弟节点**的编号序列，而本用例构造的是**链式嵌套**（每层唯一
        子节点 → id 形如 "1.1.1...1"，末段恒为 1）。现按层校验
        ``level == i+1`` 与 ``id == "1."*i+"1"``，回到 docstring 的原始意图。
        """
        from app.services.numbering import renumber_outline_nodes

        DEPTH = 120  # 远高于常见 6~8 级；必须完整编号而非静默截断
        chain: dict = {"title": "根"}
        cur = chain
        for i in range(DEPTH):
            nxt: dict = {"title": f"第{i + 1}层"}
            cur["children"] = [nxt]
            cur = nxt
        cur["children"] = []

        # 不得抛 RecursionError / KeyError（末端节点无编号即静默截断）
        out = renumber_outline_nodes([chain])
        node, walked = out[0], []
        for i in range(DEPTH + 1):
            walked.append(node.get("id"))
            assert node.get("id") == "1." * i + "1", (
                f"第 {i} 层编号与深度不对齐: {node.get('id')!r}（D-1 多级编号失步）"
            )
            assert node.get("level") == i + 1, (
                f"第 {i} 层 level 与深度不对齐: {node.get('level')!r}（D-2 层级错位）"
            )
            if i < DEPTH:
                node = node["children"][0]
        assert len(walked) == DEPTH + 1 and all(walked), (
            f"深层节点存在无编号（递归被截断）: 共遍历 {len(walked)} 层"
        )

    def test_five_level_tree_level_equals_id_depth(self):
        """D-2：5 级树整体校验 ``level == len(id.split('.'))``。

        renumber_section_outline_ids 的返回三元组是
        (outline_json, level, section_id)，下游按 level 决定 Word 标题样式。
        若某层 level 与 id 深度不一致，导出即出现「1.2.3.4」显示为
        Heading 3 的错层，目录页与正文页不匹配。
        """
        from app.services.numbering import renumber_section_outline_ids

        def build(depth: int, prefix: str = "1") -> list:
            nodes = []
            for i in range(3):
                sid = f"{prefix}.{i + 1}"
                node = {"id": f"r{prefix}_{i}",
                        "outline_json": json.dumps(
                            {"id": "旧编号", "confidence": 0.7}),
                        "children": []}
                if depth > 1:
                    node["children"] = build(depth - 1, sid)
                nodes.append(node)
            return nodes

        updates = renumber_section_outline_ids(build(5))
        assert len(updates) >= 1, "树为空"
        for outline_json, level, _section_id in updates:
            oid = json.loads(outline_json)["id"]
            assert level == len(oid.split(".")), (
                f"level={level} 与编号深度 {len(oid.split('.'))} 不一致"
                f"（D-2）: {oid}"
            )

    def test_confidence_preserved_through_five_levels(self):
        """D-2 附属：级联重排不得丢失既有字段（confidence / note 等元数据）。"""
        from app.services.numbering import renumber_section_outline_ids

        tree = [
            {"id": "a", "outline_json": json.dumps(
                {"id": "9", "confidence": 0.95, "note": "人工标注"}), "children": [
                {"id": "b", "outline_json": json.dumps(
                    {"id": "9.1", "confidence": 0.6}), "children": [
                    {"id": "c", "outline_json": json.dumps({"id": "9.1.1"}),
                     "children": []},
                ]},
            ]},
        ]
        updates = renumber_section_outline_ids(tree)
        assert len(updates) == 3
        oj0, oj1, oj2 = (json.loads(u[0]) for u in updates)
        assert oj0["confidence"] == 0.95 and oj0["note"] == "人工标注"
        assert oj1["confidence"] == 0.6
        assert (oj0["id"], oj1["id"], oj2["id"]) == ("1", "1.1", "1.1.1")


# ============================================================
# D-3：标题修改后编号必须重算（级联平移）
# ============================================================
@_REQUIRES_APP
class TestRenumbrAfterTitleEdit:
    """目录编辑（插入 / 删除 / 改挂）后的编号级联正确性。

    用户症状：在第一章前插入一节，原有 2~N 章仍是旧编号，直到导出预检
    （``_detect_section_number_mismatch``）才报警——此时「修改」已发生、
    「编号」未重算，两处口径分裂。
    """

    @staticmethod
    def _siblings(n: int) -> list:
        return [{"id": f"s{i}", "outline_json": json.dumps({"id": "旧"}),
                 "children": [{"id": f"s{i}_c",
                               "outline_json": json.dumps({"id": "旧"}),
                               "children": []}]}
                for i in range(n)]

    def test_insert_at_front_cascades_all_numbers(self):
        """D-3a：在第 0 位插入新节点，原有节点整体 +1 平移。"""
        from app.services.numbering import renumber_section_outline_ids

        tree = self._siblings(4)
        tree.insert(0, {"id": "new", "outline_json": None, "children": []})
        ids = [json.loads(u[0])["id"] for u in renumber_section_outline_ids(tree)]
        assert ids[0] == "1", f"新插入节点未成为第一节: {ids[0]}"
        # ✅ 期望值修正（2026-10-08）：`_siblings` 的每个节点各带 1 个子节点，
        #    updates 为 DFS 序（父 → 子）。原期望 ids[1:5]==["2","3","4","5"]
        #    漏算了子节点条目，与自己的测试桩矛盾（探针锚定真实输出后修正）。
        #    级联语义不变：新节点成为 "1"，原有节点及其子节点整体 +1 平移。
        assert ids == ["1", "2", "2.1", "3", "3.1", "4", "4.1", "5", "5.1"], (
            f"插入后原有节点未级联平移（标题修改后编号未重算）: {ids}"
        )

    def test_delete_middle_cascades_remainder(self):
        """D-3b：删除中间节点，其后节点必须回补（不得断号 / 重复）。"""
        from app.services.numbering import renumber_section_outline_ids

        tree = self._siblings(5)
        del tree[2]
        ids = [json.loads(u[0])["id"] for u in renumber_section_outline_ids(tree)]
        # ✅ 期望值修正（2026-10-08）：同 D-3a —— 含子节点的 DFS 全序
        #    （每个节点 1 个子节点，共 8 条 updates）。
        assert ids == ["1", "1.1", "2", "2.1", "3", "3.1", "4", "4.1"], (
            f"删除中间节点后出现断号或重复（含子节点回补）: {ids}"
        )

    def test_child_reparent_resyncs_level_and_number(self):
        """D-3c：子节点改挂到另一父节点后，编号与 level 必须按新位置重算。"""
        from app.services.numbering import renumber_section_outline_ids

        a = {"id": "a", "outline_json": json.dumps({"id": "1"}), "children": []}
        b = {"id": "b", "outline_json": json.dumps({"id": "2"}), "children": []}
        child = {"id": "c", "outline_json": json.dumps({"id": "1.1"}),
                 "children": []}
        tree = [a, b]
        a["children"] = []          # 从 a 下摘走
        b["children"] = [child]     # 挂到 b 下
        updates = renumber_section_outline_ids(tree)
        by_section = {u[2]: (json.loads(u[0])["id"], u[1]) for u in updates}
        assert by_section["a"] == ("1", 1)
        assert by_section["b"] == ("2", 1)
        assert by_section["c"] == ("2.1", 2), (
            f"子节点改挂后编号/层级未同步: {by_section['c']}"
        )

    def test_invalid_section_number_is_noop_not_crash(self):
        """D-8a：空数据 / 非法章节号不得抛错，必须原样返回。

        根因（2026-10-08 修）：存储编号合法性曾在本模块有**两份实现** ——
        ``get_stored_section_id`` 用 canonical 的 ``_DOT_PATH_RE.fullmatch``，
        而 ``stored_id_to_display`` / ``stored_id_to_prefix`` 各自用
        ``str(...).split(".") + isdigit`` 松过滤，把 float ``3.5`` 抢救成
        ``"5"``、把 ``"abc1.2"`` 抢救成 ``"2"`` —— 非法章节号于是**静默改写**
        落库正文（`## 总体安排` 被写成 `## 5.1 总体安排`）。已收敛为唯一判据
        ``_stored_id_parts``。
        """
        from app.services.numbering import (
            _stored_id_parts,
            renumber_section_body_subheadings,
            stored_id_to_display,
            stored_id_to_prefix,
        )

        content = "## 总体安排\n"
        for bad in ("", None, "6fa8-uuid", "abc", -1, 3.5):
            new, changes = renumber_section_body_subheadings(content, bad, 2)
            assert new == content and changes == [], (
                f"非法章节号 {bad!r} 未走 no-op 分支"
            )

        # 单一判据护栏：类型不符 / 形态不符 / 0 号段一律非法（不得放宽）
        for bad in ("", None, "6fa8-uuid", "abc", -1, 3.5, "abc1.2", 0, True,
                    ["1.2"], {"id": 3}):
            assert _stored_id_parts(bad) == [], f"非法编号 {bad!r} 未被判非法"
            assert stored_id_to_prefix(bad) == "" and \
                stored_id_to_display(bad) == "", (
                    f"非法编号 {bad!r} 仍被折算出编号（松过滤回流）")

        # 合法形态不得因判据收紧而回归
        assert stored_id_to_prefix("3.2") == "2"
        assert stored_id_to_prefix("1") == "1"
        assert stored_id_to_prefix(3) == "3"          # int 形态（JSON {"id": 3}）
        assert stored_id_to_display("3.2.4") == "2.4"
        assert stored_id_to_display("1") == "第一章"

        # 静态防分叉：两个派生函数不得各自保留判据副本
        import app.services.numbering as nb
        bodies = inspect.getsource(nb.stored_id_to_display) + \
            inspect.getsource(nb.stored_id_to_prefix)
        assert "isdigit" not in bodies and "_DOT_PATH_RE" not in bodies, (
            "D-8a：判据重新分叉为两份实现（应只走 _stored_id_parts）")
        assert "_stored_id_parts" in bodies, "D-8a：判据唯一出口被摘掉"

    def test_empty_tree_and_all_empty_titles(self):
        """D-8b：空树 / 全空标题节点返回空结果且不抛错。"""
        from app.services.numbering import renumber_outline_nodes, renumber_section_outline_ids

        assert renumber_outline_nodes([]) == []
        nodes = [{"title": "", "children": None}, {"title": None}, None]
        out = renumber_outline_nodes(nodes)
        kept = [n for n in out if isinstance(n, dict)]
        assert all(isinstance(n.get("children"), list) for n in kept), (
            "children 未规范化为 list（空数据分支缺陷）"
        )
        # DB 侧：outline_json 为 None 的节点仍需产出重排条目
        updates = renumber_section_outline_ids(
            [{"id": "x", "outline_json": None, "children": []}])
        assert len(updates) == 1 and json.loads(updates[0][0])["id"] == "1"


# ============================================================
# D-7：编号命名空间隔离（正文子标题 vs DB 子章节）
# ============================================================
@_REQUIRES_APP
class TestNumberingNamespaceIsolation:
    """统一编号命名空间：正文子标题降级后，任何深度都不得产出点分编号。

    冲突根源（既有 §11 已修复）：有 DB 子章节的章节，正文子标题与 DB 子章节
    共用「X.Y」会撞号。既有测试只断言了 rel0/rel1/rel2 三档输出形态，
    没有断言「rel>=2 的全部层级都在 L6/L7 命名空间内、永不出现 X.Y」。
    """

    @staticmethod
    def _call(rel_levels, has_children=True):
        from app.routers.export import _compute_subheading
        sub: dict = {}
        outs = []
        for rel in rel_levels:
            text, style = _compute_subheading("2", 2, rel, sub, f"T{rel}",
                                              sec_id="sec-1",
                                              has_children=has_children)
            outs.append((text, style))
        return outs

    def test_no_dot_numbering_when_demoting(self):
        """D-7a：降级开启时，rel 1..6 全部输出不得含『数字.数字』点分编号。"""
        outs = self._call([1, 2, 3, 4, 5, 6], has_children=True)
        dotted = [t for t, _ in outs
                  if "." in t.split()[0].strip("、）")]
        assert not dotted, (
            f"降级后仍产出点分编号（与 DB 子章节命名空间冲突）: {dotted}"
        )
        assert all(s in (6, 7) for _, s in outs), (
            f"降级样式越界（会与章节级 Heading 撞样式）: {[s for _, s in outs]}"
        )

    def test_demote_toggle_is_the_single_switch(self):
        """D-7b：同一输入，降级开关两侧输出差异必须完全由该开关决定。"""
        on = self._call([1, 2, 3], has_children=True)
        off = self._call([1, 2, 3], has_children=False)
        assert on != off, "两种命名空间输出应不同（开关失效）"
        assert "." in off[0][0], f"未降级侧应产出点分编号: {off[0][0]}"
        assert on[0][0].startswith("1）"), f"降级侧 rel0 应为『1）、』: {on[0][0]}"


# ============================================================
# D-4 / D-5：长方案分步生成状态机
# ============================================================
@_REQUIRES_APP
class TestOutlineStepGenerationState:
    """目录生成分步（checkpoint）状态机：终态清理与落库失败的降级。

    既有 G12-5 把「finish_task 内存态必须清理」守卫绑定在
    ``task_type="content_generation"`` 上。目录生成走同一个 task_registry，
    ``outline_generation`` 的等价路径此前无任何测试 —— 一旦 finish_task 里
    出现按 type 分支的特判，目录生成就会重新产生「任务在 DB 里永远 running」
    （``logs/backend.log`` 2026-09-23 trace=9fe4498339da 已出现过该类事故）。
    """

    @staticmethod
    def _add_task(tid: str, task_type: str = "outline_generation") -> None:
        import asyncio

        import app.services.ai.task_registry as tr
        tr._tasks[tid] = {
            "type": task_type, "status": "running", "scheme_id": "sc1",
            "pause_event": asyncio.Event(), "stop_event": asyncio.Event(),
            "child_tasks": set(),
        }

    @pytest.mark.asyncio
    async def test_outline_generation_task_popped_when_db_write_fails(
            self, db_conn, monkeypatch):
        """D-4：outline_generation 终态写库失败时，_tasks 仍必须清理。

        与 G12-5 相同的失败模式（``database is locked`` / 镜像损坏），
        但任务类型换成目录生成。若此处残留，目录生成任务永远显示 running，
        前端 pollTaskUntilTerminal 死等。
        """
        import app.services.ai.task_registry as tr

        tid = "outline-t-db-broken"
        self._add_task(tid)

        async def _boom(*a, **k):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(tr, "get_conn", _boom)
        await tr.finish_task(tid, "stopped", "用户已停止")
        assert tid not in tr._tasks, (
            "目录生成任务终态写库失败后仍残留 _tasks（D-4，与 G12-5 同源）"
        )

    @pytest.mark.asyncio
    async def test_outline_generation_task_popped_when_broadcast_fails(
            self, db_conn, monkeypatch):
        """D-4 广播失败分支：同样不得残留。"""
        import app.services.ai.task_registry as tr

        tid = "outline-t-broadcast-broken"
        self._add_task(tid)

        async def _boom(*a, **k):
            raise RuntimeError("订阅者队列异常")

        monkeypatch.setattr(tr, "broadcast", _boom)
        await tr.finish_task(tid, "stopped", "客户端断开")
        assert tid not in tr._tasks

    @pytest.mark.asyncio
    async def test_terminal_persistence_failure_is_idempotent(
            self, db_conn, monkeypatch):
        """D-4 附属：DB 锁竞争下 finish_task 重复调用必须幂等且不残留。"""
        import app.services.ai.task_registry as tr

        tid = "outline-t-locked"
        self._add_task(tid)

        async def _boom(*a, **k):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(tr, "get_conn", _boom)
        await tr.finish_task(tid, "failed", "落库失败")
        await tr.finish_task(tid, "failed", "落库失败")  # 重复调用必须幂等
        assert tid not in tr._tasks

    async def test_update_progress_entrypoint_exists_and_fails_soft(
            self, monkeypatch, caplog):
        """D-5：分步生成进度落库入口必须存在，且写库失败不得向外抛错。

        依据：``app/db.py`` 头部注释记录的真实生产事故「task_registry
        .update_progress 落库失败 ×4、保存目录生成 checkpoint 失败、
        finish_task 终态落库失败: database is locked」。

        ✅ 用例修正（2026-10-08）：原用例只按 task_id 形态构造 kwargs，
        漏传**必填参数** ``progress`` → 调用绑定期 TypeError，把「签名不匹配」
        误报成「fail-soft 缺失」；且 sync 用例里 ``asyncio.get_event_loop()
        .run_until_complete`` 在无运行循环时不可用。现改为：
        按真实签名补齐全部必填参数（inspect 锚定，不假设内部形态）→
        monkeypatch get_conn 打爆 DB → await 调用 → 断言不抛 + 内存态仍更新
        + 落库失败走了结构化 WARNING（fail-soft 三要素缺一不可）。
        """
        import logging

        import app.services.ai.task_registry as tr

        if not hasattr(tr, "update_progress"):
            pytest.skip(
                "缺口确认（D-5）：task_registry 未暴露 update_progress，"
                "分步生成进度落库入口缺失 —— 目录生成 checkpoint 无法持久化。"
            )
        tid = "outline-t-progress"
        self._add_task(tid)
        # 按真实签名构造最小调用：必填参数全部补齐，可选用默认值
        sig = inspect.signature(tr.update_progress)
        dummies = {"task_id": tid, "id": tid, "tid": tid, "progress": 42.0,
                   "message": "", "event": "progress", "force": True}
        kwargs: dict = {}
        for name, p in sig.parameters.items():
            if p.default is not inspect.Parameter.empty:
                continue
            if p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD,
                              p.KEYWORD_ONLY):
                continue
            if name not in dummies:
                pytest.fail(
                    f"D-5：update_progress 出现未预期的必填参数 {name!r}，"
                    "请补齐测试桩后再验证 fail-soft（禁止跳过）")
            kwargs[name] = dummies[name]
        assert "task_id" in kwargs and "progress" in kwargs

        async def _boom(*a, **k):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(tr, "get_conn", _boom)
        with caplog.at_level(logging.WARNING, logger="task_registry"):
            # DB 写失败（含 retry_db_op 3 次退避后仍失败）不得向外抛
            await tr.update_progress(**kwargs)
        # fail-soft 三要素：① 不抛（能走到这里即通过）；② 内存态仍更新
        # （SSE 广播链不中断）；③ 失败落结构化 WARNING 可观测。
        assert tr._tasks[tid]["progress"] == 42.0
        assert any("落库失败" in r.getMessage() for r in caplog.records), (
            "D-5：进度落库失败未落 WARNING（静默吞掉，排障不可见）"
        )


# ============================================================
# D-6：跨模块数据传递（目录生成 → 正文生成）
# ============================================================
@_REQUIRES_APP
class TestCrossModuleOutlineHandoff:
    """目录生成 checkpoint 被正文生成正常消费的正向路径。

    既有 test_content_generation_g12 只覆盖了 ``_attach_checkpoint_result``
    的**跳过**分支：task_type 不是 content_generation → 跳过；outline 为空
    → 跳过。从未有人断言「合法的 outline_result 会被真正附上」。
    若正向路径被静默丢弃，症状只是「正文生成用不到目录」—— 无异常、
    无日志、无告警，是最典型的跨模块数据传递丢失。
    """

    @staticmethod
    def _outline() -> list:
        return [
            {"id": "1", "title": "工程概况", "level": 1, "children": []},
            {"id": "2", "title": "编制依据", "level": 1, "children": [
                {"id": "2.1", "title": "法规依据", "level": 2, "children": []},
            ]},
        ]

    def test_valid_outline_result_is_attached(self):
        """D-6：合法 outline_result 必须产生非空结果（不得静默丢弃）。

        ✅ 用例修正（2026-10-08）：原用例把 outline_result 塞进
        ``task_type="content_generation"`` 的行 —— 而白名单
        ``_CHECKPOINT_KINDS`` 明确约定 outline_result 只属于
        ``outline_generation``，content_generation 只接受 content_result
        （第 7497 行 kind 不匹配即跳过，属**正确**的跳过而非缺陷）。
        现在按合法配对构造，正向分支（第 7499 行）真实被执行到，
        并额外锁定白名单语义：内部字段不得透传给前端。
        """
        import app.routers.sse_handlers as sh

        outline = self._outline()
        row = {"task_type": "outline_generation",
               "checkpoint_json": json.dumps({
                   "kind": "outline_result", "outline": outline,
                   "event": "completed",
                   # 内部字段：白名单必须过滤掉，不得回传给前端
                   "_internal_debug": "必须不出现"})}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        assert result, (
            "缺口确认（D-6）：合法 outline_result 未被附上，目录→正文的"
            "跨模块传递丢失 —— _attach_checkpoint_result 正向消费路径未接线。"
        )
        payload = result["outline_result"]
        assert payload.get("outline") == outline, (
            "D-6：outline 载荷被改写或压扁，目录结构丢失")
        assert payload.get("event") == "completed", "D-6：event 字段被过滤"
        assert "_internal_debug" not in payload, (
            "D-6：内部字段未经白名单过滤，直接泄漏给前端")

    def test_cross_kind_checkpoint_is_skipped_by_design(self):
        """D-6 附属：跨 task_type 的 checkpoint 必须跳过（防止「放松」误修）。

        这是 _CHECKPOINT_KINDS 的配对契约（outline_result ↔ outline_generation、
        content_result ↔ content_generation）。若有人为了让正向用例通过而
        放宽 kind 校验，本用例会失败 —— 与 D-6 主用例互为反向护栏。
        """
        import app.routers.sse_handlers as sh

        row = {"task_type": "content_generation",
               "checkpoint_json": json.dumps({
                   "kind": "outline_result", "outline": self._outline()})}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        assert result == {}, (
            "content_generation 任务不应收到 outline_result（跨类型泄漏）")

    def test_handoff_preserves_titles_and_numbering(self):
        """D-6 附属：附上后的结构必须保留编号形态与节点文本（不得被压扁）。"""
        import app.routers.sse_handlers as sh

        outline = [{"id": "3.2", "title": "进度计划", "level": 2, "children": [
            {"id": "3.2.1", "title": "总体安排", "level": 3, "children": []}]}]
        row = {"task_type": "outline_generation",
               "checkpoint_json": json.dumps(
                   {"kind": "outline_result", "outline": outline})}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)
        # 不再 pytest.skip：正向路径已按合法配对接线，空结果即真缺陷
        assert result, "D-6：正向路径未接线（outline_result 被静默丢弃）"
        # 注意：result 是调用方传入的累积 dict（原地写入），不能再 json.loads
        assert result["outline_result"]["outline"] == outline, (
            "D-6：目录结构在传递中被改写")
        blob = json.dumps(result, ensure_ascii=False)
        assert "进度计划" in blob and "总体安排" in blob, "目录节点文本丢失"
        assert "3.2" in blob or "3.2.1" in blob, "目录编号形态丢失"

    def test_corrupt_checkpoint_json_is_swallowed(self):
        """D-6 附属：损坏的 checkpoint_json 不得让正文生成整体失败。"""
        import app.routers.sse_handlers as sh

        row = {"task_type": "content_generation", "checkpoint_json": "{坏 JSON"}
        result: dict = {}
        sh._attach_checkpoint_result(row, result)  # 不得抛异常
        assert isinstance(result, dict)


# ============================================================
# D-8：超长内容 / 编码问题（正文子标题编号规范化）
# ============================================================
@_REQUIRES_APP
class TestEncodingAndLargePayload:
    """编码与超长内容：BOM / CRLF / 全角数字 / 大体积正文。

    修复重点第 10 项。既有覆盖只有「围栏/列表/段落不动」「空数据 no-op」，
    没有任何一条针对字节层编码形态。
    """

    def test_bom_does_not_block_first_heading(self):
        """D-8c：UTF-8 BOM 不得阻止首行标题被识别与重编号。

        Windows 记事本 / 部分 OCR 输出常带 BOM。若 BOM 黏在 ``#`` 前，
        标题正则不匹配 → 首节正文标题保持无编号，导出目录少一条。
        """
        from app.services.numbering import renumber_section_body_subheadings

        content = "\ufeff## 总体安排\n\n## 资源配置\n"
        new, _ = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "2.1" in new and "2.2" in new, (
            f"D-8c：BOM 导致标题未被编号（编号未重算）: {new[:80]!r}"
        )

    def test_crlf_line_endings_keep_changes_line_accurate(self):
        """D-8d：CRLF 换行下 changes[].line 必须仍是准确的 1 基行号。

        line 号是「精确重写源行」的定位依据（见 test_numbering_unification §6
        heading src_line）。若按 ``\\n`` 切分而原文是 ``\\r\\n``，行号会整体
        偏移，后续按行回填会写错行。
        """
        from app.services.numbering import renumber_section_body_subheadings

        crlf = "## 总体安排\r\n\r\n正文段落。\r\n\r\n## 资源配置\r\n"
        lf = crlf.replace("\r\n", "\n")
        _, changes_crlf = renumber_section_body_subheadings(
            crlf, "3.2", 2, "进度计划")
        _, changes_lf = renumber_section_body_subheadings(lf, "3.2", 2, "进度计划")
        assert len(changes_crlf) == len(changes_lf) == 2, (
            f"CRLF 与 LF 识别出的标题数不一致: "
            f"{len(changes_crlf)} vs {len(changes_lf)}"
        )
        assert [c["line"] for c in changes_crlf] == \
               [c["line"] for c in changes_lf], "CRLF 下行号偏移（D-8d）"

    def test_fullwidth_digit_heading_is_numbered(self):
        """D-8e：全角数字标题（如『２ 总体安排』）必须能被规范化。

        用户从 Word / PDF 复制时全角化很常见；既有
        test_plain_heading_and_bold_rewritten 只覆盖半角。
        """
        from app.services.numbering import renumber_section_body_subheadings

        content = "２ 总体安排\n\n**３ 资源配置**\n"
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "2.1" in new, f"D-8e：全角数字标题未被编号: {new[:60]!r}"
        assert len(changes) >= 1

    def test_large_single_section_body_is_idempotent(self):
        """D-8f：单章超大正文（≈200KB）规范化必须幂等且不退化。

        既有性能护栏（test_numbering_batch_perf）锁的是**查询次数**，
        不是单章正文的文本处理复杂度。正文越长越容易出现 O(n²) 回写。
        这里用「二次运行零改动」的确定性断言代替时间断言（与仓库风格一致）。
        """
        from app.services.numbering import renumber_section_body_subheadings

        body = ("### 子标题{}\n\n" + "正文段落。" * 80 + "\n\n") * 300
        content = "## 总体安排\n\n" + body
        once, _ = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        twice, changes2 = renumber_section_body_subheadings(
            once, "3.2", 2, "进度计划")
        assert twice == once, "超长正文规范化不幂等（二次运行仍在改）"
        assert changes2 == [], f"超长正文二次运行仍有 {len(changes2)} 处改动"

    def test_heading_inside_code_fence_with_unicode_is_untouched(self):
        """D-8g：围栏内 unicode / 全角标题一律不动（既有护栏的编码变体）。"""
        from app.services.numbering import renumber_section_body_subheadings

        content = (
            "```markdown\n＃＃ 围栏内全角井号\n## 围栏内标题\n```\n\n"
            "## 真标题\n"
        )
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "＃＃ 围栏内全角井号" in new and "## 围栏内标题" in new
        assert len(changes) == 1





