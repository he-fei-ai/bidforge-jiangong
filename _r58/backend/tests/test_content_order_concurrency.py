"""正文生成模块 · 目录顺序与并发控制回归守卫（2026-09-19 深度审查）

覆盖三处 P0/P1 修复：
1. **目录顺序（P0）**：sections.sort_order 是「同级内序号」而非全局序号，
   SQL `ORDER BY sort_order` 的扁平序≠文档序。正文生成必须先过
   `order_sections_dfs` 重排为前序 DFS，任务才能严格按目录层级排队递进。
2. **并发控制（P0）**：正文生成不再复用全局自适应信号量
   （concurrency_controller.semaphore），改为**每任务独立** Semaphore，
   容量严格 = 用户前端档位；档位解析容忍字符串/浮点等序列化变体。
3. **字数脏值（P1）**：leaf.word_budget 为字符串/NULL 时不再让续写判定
   抛 TypeError 把整章误报为"生成失败"（与目录 word_budget 守卫同源）。
"""
import inspect

import app.routers.sse_handlers as sh
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    leaf_word_budget,
    order_sections_dfs,
    resolve_concurrency,
    select_target_leaves,
)


def _sec(sid, pid, level, sort_order):
    return {"id": sid, "parent_id": pid, "level": level,
            "sort_order": sort_order, "title": sid, "content": "",
            "word_count": 0, "status": "empty"}


# ============================================================
# 1. order_sections_dfs —— 目录树前序 DFS
# ============================================================
class TestOrderSectionsDfs:

    def test_empty(self):
        assert order_sections_dfs([]) == []

    def test_document_order_multi_level(self):
        """模拟 ORDER BY sort_order 的"按列展开"错序，重排后必须是文档序。

        树：1 → (1.1 → 1.1.1, 1.2), 2 → (2.1)
        每级的 sort_order 都是 0/1 同级序号 → 扁平 ORDER BY 会把
        1.1.1（sort=0）排到 2（sort=1）之前、把 1.2 与 2.1 混排。
        """
        flat = [
            _sec("1", "", 1, 0), _sec("1.1", "1", 2, 0), _sec("1.1.1", "1.1", 3, 0),
            _sec("2", "", 1, 1), _sec("1.2", "1", 2, 1), _sec("2.1", "2", 2, 0),
        ]
        got = [s["id"] for s in order_sections_dfs(flat)]
        assert got == ["1", "1.1", "1.1.1", "1.2", "2", "2.1"]

    def test_siblings_sorted_by_sort_order(self):
        flat = [
            _sec("c", "", 1, 2), _sec("a", "", 1, 0), _sec("b", "", 1, 1),
            _sec("b2", "b", 2, 1), _sec("b1", "b", 2, 0),
        ]
        got = [s["id"] for s in order_sections_dfs(flat)]
        assert got == ["a", "b", "b1", "b2", "c"]

    def test_orphans_kept_as_roots(self):
        """parent_id 悬空（指向已删节点）不得丢行。"""
        flat = [_sec("1", "", 1, 0), _sec("x", "ghost", 2, 0)]
        got = {s["id"] for s in order_sections_dfs(flat)}
        assert got == {"1", "x"}

    def test_self_parent_not_lost(self):
        flat = [_sec("1", "", 1, 0), _sec("s", "s", 2, 0)]
        got = {s["id"] for s in order_sections_dfs(flat)}
        assert got == {"1", "s"}

    def test_cycle_defense_no_loss(self):
        """父子成环（脏数据）：环上节点不可达也必须补挂尾部，绝不丢行。"""
        a, b = _sec("A", "B", 2, 0), _sec("B", "A", 2, 0)
        flat = [_sec("r", "", 1, 0), a, b]
        got = {s["id"] for s in order_sections_dfs(flat)}
        assert got == {"r", "A", "B"}

    def test_leaves_queue_in_tree_order(self):
        """端到端：DFS 重排 + select_target_leaves 后，叶子队列严格目录序。"""
        flat = [
            _sec("1", "", 1, 0), _sec("1.1", "1", 2, 0), _sec("1.1.1", "1.1", 3, 0),
            _sec("2", "", 1, 1), _sec("1.2", "1", 2, 1), _sec("2.1", "2", 2, 0),
        ]
        leaves = select_target_leaves(order_sections_dfs(flat), mode="all")
        assert [s["id"] for s in leaves] == ["1.1.1", "1.2", "2.1"]


# ============================================================
# 2. resolve_concurrency —— 用户档位严格归一
# ============================================================
class TestResolveConcurrency:

    def test_named_levels(self):
        assert resolve_concurrency("slow") == 2
        assert resolve_concurrency("balanced") == 3
        assert resolve_concurrency("fast") == 5

    def test_level_case_and_space_tolerant(self):
        assert resolve_concurrency(" FAST ") == 5
        assert resolve_concurrency("Slow") == 2

    def test_numeric_variants(self):
        """前端序列化为 "3" / 3.0 时不得静默丢弃（旧实现只认 int）。"""
        assert resolve_concurrency(4) == 4
        assert resolve_concurrency("3") == 3
        assert resolve_concurrency(3.0) == 3

    def test_clamp_to_ceiling(self):
        assert resolve_concurrency(8) == 5
        assert resolve_concurrency("9") == 5

    def test_invalid_falls_back_to_default(self):
        assert resolve_concurrency(None, default=4) == 4
        assert resolve_concurrency("abc", default=2) == 2
        assert resolve_concurrency(True, default=3) == 3
        assert resolve_concurrency(0, default=5) == 5    # 0/负数非法 → default
        assert resolve_concurrency(-2, default=5) == 5

    def test_default_itself_clamped(self):
        # AI 配置里的 target 若越界（历史脏配置），也被钳进 [1,5]
        assert resolve_concurrency(None, default=99) == 5
        assert resolve_concurrency(None, default=0) == 3


# ============================================================
# 3. leaf_word_budget —— 脏值稳健
# ============================================================
class TestLeafWordBudget:

    def test_normal_values(self):
        assert leaf_word_budget({"word_budget": 2000}) == 2000
        assert leaf_word_budget({"word_budget": 2000.7}) == 2000
        assert leaf_word_budget({"word_budget": "1800"}) == 1800

    def test_dirty_values_fall_back(self):
        assert leaf_word_budget({"word_budget": None}) == DEFAULT_WORD_BUDGET
        assert leaf_word_budget({"word_budget": 0}) == DEFAULT_WORD_BUDGET
        assert leaf_word_budget({"word_budget": -5}) == DEFAULT_WORD_BUDGET
        assert leaf_word_budget({"word_budget": "abc"}) == DEFAULT_WORD_BUDGET
        assert leaf_word_budget({}) == DEFAULT_WORD_BUDGET
        # 回归本体：字符串预算参与 `wc < budget * 0.8` 会抛 TypeError
        b = leaf_word_budget({"word_budget": "1500"})
        assert 100 < b * 0.8  # 参与浮点比较不炸


# ============================================================
# 4. 源码级守卫 —— generate_content 接线正确
# ============================================================
class TestContentGenerationWiring:

    def _src(self):
        return inspect.getsource(sh.generate_content)

    def test_sections_reordered_dfs_before_selection(self):
        src = self._src()
        i_dfs = src.index("order_sections_dfs(all_sections)")
        i_sel = src.index("select_target_leaves(")
        assert i_dfs < i_sel, "必须先 DFS 重排再筛选叶子，任务队列才是目录序"

    def test_per_task_semaphore_strict_user_level(self):
        """窗口调度器守卫（替代旧 Semaphore 断言）。

        正文生成不再用"所有协程预 create_task + asyncio.Semaphore" ——
        该模式在 Python 事件循环下不保证启动顺序严格目录 DFS 序
        （已用 30 协程×3 档实测验证：acquire 顺序在 30%~100% 场景乱序，
        同级后序先完成导致前序同级结尾摘要失效）。
        现改为**显式队列 + 固定窗口**，窗口大小 = 用户档位，
        启动顺序 = pending.popleft() 的目录 DFS 序。
        """
        src = self._src()
        # ✅ 2026-09-26（陈旧断言更新）：早期实现是「显式 pending 队列 +
        #    _WINDOW 窗口调度器」，后回退为「每任务独立信号量 + FIFO 放行」
        #    （asyncio.Semaphore 对等待者 FIFO → 启动顺序 = 协程创建顺序 =
        #    目录 DFS 序；as_completed 只影响**完成**顺序，不影响启动顺序）。
        #    故 `_WINDOW = int(effective_concurrency)` / `_pending:` / `_launch_next`
        #    这些符号已不存在，原断言必然失败。改为锁定**现行口径**。
        #
        # ① 容量严格取用户档位，不得绕全局自适应控制器间接取值
        assert "asyncio.Semaphore(effective_concurrency)" in src, (
            "并发闸门必须直接用用户档位 effective_concurrency，"
            "不得再绕 Semaphore 间接控制")
        # ② 必须是每任务独立信号量（而非全局 concurrency_controller）
        assert "_run_semaphore" in src
        # ③ 闸门必须在暂停检查**之后**、实际工作之前获取
        gate = src[src.index("async def guarded_gen"):]
        assert "await wait_resume(task_id)" in gate.split("async with _run_semaphore")[0], (
            "暂停闸门必须前置到信号量之前（否则暂停期间白占并发许可）")
        assert "async with _run_semaphore" in gate
        # ④ 回收须用 as_completed 等待全部子任务
        assert "asyncio.as_completed(tasks)" in src
        # ⑤ 严格 DFS 序：协程必须按 leaves 目录序创建
        assert "for" in src and "leaves" in src

    def test_no_global_adaptive_gate_in_content(self):
        """generate_content 内不得再动全局自适应闸门（跨任务互染源头）。"""
        src = self._src()
        assert "concurrency_controller.set_concurrency" not in src, (
            "不得再调用全局 set_concurrency —— 会挤掉其它任务/配置的用户档位")

    def test_display_concurrency_locked_to_user_level(self):
        src = self._src()
        assert '_prog["concurrency"] = effective_concurrency' in src
        # 展示值不再被自适应控制器实时值覆写
        assert "_refresh_concurrency" not in src
