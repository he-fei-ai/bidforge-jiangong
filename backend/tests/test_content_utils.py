"""正文生成目标章节筛选 单元测试

重点覆盖修复项：
- 默认（非 force_rewrite）必须跳过【已有非空正文】的章节，
  而不仅是 status=='generated' —— reviewed/expanded 等已人工处理章节
  一旦有正文，绝不能被默认生成覆盖。
"""
from app.services.content_utils import (
    select_target_leaves, build_sibling_context, word_status_for, text_word_count,
    normalize_word_budget_override, resolve_concurrency,
)
from app.routers.sse_handlers import _apply_word_budget_allocations


def _s(sid, title, parent="", level=1, status="empty",
       content="", word_count=0, sort_order=0):
    return {"id": sid, "title": title, "parent_id": parent, "level": level,
            "status": status, "content": content,
            "word_count": word_count, "sort_order": sort_order}


def _tree():
    """A → A1 → (A1a, A1b)；A → A2；B → B1。叶子应为 A1a/A1b/A2/B1"""
    return [
        _s("A", "第一章", level=1, sort_order=0),
        _s("A1", "1.1", parent="A", level=2, sort_order=1),
        _s("A1a", "1.1.1", parent="A1", level=3, sort_order=2),
        _s("A1b", "1.1.2", parent="A1", level=3, sort_order=3),
        _s("A2", "1.2", parent="A", level=2, sort_order=4),
        _s("B", "第二章", level=1, sort_order=5),
        _s("B1", "2.1", parent="B", level=2, sort_order=6),
    ]


def _ids(items):
    return [s["id"] for s in items]


class TestSelectTargetLeaves:
    def test_empty(self):
        assert select_target_leaves([]) == []

    def test_all_leaves_when_no_content(self):
        assert _ids(select_target_leaves(_tree())) == ["A1a", "A1b", "A2", "B1"]

    def test_skip_generated_with_content(self):
        sections = _tree()
        sections[2].update(status="generated", content="已有正文", word_count=800)
        assert "A1a" not in _ids(select_target_leaves(sections))

    def test_skip_reviewed_with_content(self):
        """✅ 核心修复：reviewed（已人工审核）章节有正文时不得被默认覆盖。"""
        sections = _tree()
        sections[3].update(status="reviewed", content="人工审核过的正文", word_count=900)
        assert "A1b" not in _ids(select_target_leaves(sections))

    def test_skip_expanded_with_content(self):
        sections = _tree()
        sections[3].update(status="expanded", content="扩写后的正文", word_count=1200)
        assert "A1b" not in _ids(select_target_leaves(sections))

    def test_force_rewrite_includes_all(self):
        sections = _tree()
        for s in sections:
            if s["id"] in ("A1a", "A1b"):
                s.update(status="reviewed", content="正文", word_count=900)
        got = _ids(select_target_leaves(sections, force_rewrite=True))
        assert got == ["A1a", "A1b", "A2", "B1"]


class TestNonFiniteNumericInputs:
    """非法浮点文本不得击穿整条正文任务。"""

    def test_word_budget_non_finite_falls_back(self):
        for raw in (float("inf"), float("-inf"), float("nan"), "Infinity", "1e309", "NaN"):
            assert normalize_word_budget_override(raw) is None

    def test_concurrency_non_finite_falls_back(self):
        for raw in (float("inf"), float("-inf"), float("nan"), "Infinity", "1e309", "NaN"):
            assert resolve_concurrency(raw, default=3) == 3


class TestApplyWordBudgetAllocations:
    """回归测试：_apply_word_budget_allocations 必须写回 units（含 leaves），
    不能写回临时的 units_payload（无 leaves），否则后续 apply 阶段读取 u['alloc']
    会 KeyError，使整个正文生成任务崩溃。"""

    def _make_units(self, leaves_per_unit):
        units = {}
        idx = 0
        for uid, n in enumerate(leaves_per_unit):
            unit = {"id": f"U{uid}", "title": f"单元{uid}"}
            u = units.setdefault(unit["id"], {"unit": unit, "leaves": []})
            for _ in range(n):
                u["leaves"].append({"id": f"L{idx}"})
                idx += 1
        return units

    def test_single_leaf_units_untouched(self):
        units = self._make_units([1, 1])
        # 单叶子单元已在调用方直接分配，helper 不改写
        units["U0"]["alloc"] = {"L0": 1500}
        _apply_word_budget_allocations(units, 1500, {})
        assert units["U0"]["alloc"] == {"L0": 1500}

    def test_multi_leaf_ai_alloc_applied(self):
        units = self._make_units([1, 3])
        ai = {"L1": 600, "L2": 500, "L3": 400}  # sum == 1500
        _apply_word_budget_allocations(units, 1500, ai)
        assert units["U1"]["alloc"] == {"L1": 600, "L2": 500, "L3": 400}

    def test_multi_leaf_fallback_even_split(self):
        units = self._make_units([1, 3])
        # AI 分配不完整（缺 L3）或总和不等 → 降级均分且总和守恒
        _apply_word_budget_allocations(units, 1500, {"L1": 600, "L2": 500})
        alloc = units["U1"]["alloc"]
        assert set(alloc.keys()) == {"L1", "L2", "L3"}
        assert sum(alloc.values()) == 1500

    def test_multi_leaf_fallback_remainder(self):
        units = self._make_units([4])
        # 1500 / 4 = 375 余 0；改为 1502 验证余数摊给前 2 个
        _apply_word_budget_allocations(units, 1502, {})
        alloc = units["U0"]["alloc"]
        vals = sorted(alloc.values())
        assert vals == [375, 375, 376, 376]
        assert sum(alloc.values()) == 1502

    def test_mode_missing_only_empty_failed_pending(self):
        sections = _tree()
        sections[2].update(status="generated", content="正文", word_count=800)  # A1a 跳过
        sections[3].update(status="failed")                                      # A1b 补
        sections[4].update(status="pending")                                     # A2 补
        sections[6].update(status="reviewed", content="正文", word_count=900)    # B1 跳过
        # B（第二章）自身为空且其子节点 B1 已被排除 → B 成为叶子一并补全
        # （与旧实现语义一致：候选集合内"不是任何候选的父节点"即为叶子）
        assert _ids(select_target_leaves(sections, mode="missing")) == ["A1b", "A2", "B"]

    def test_parent_becomes_leaf_when_children_all_done(self):
        """子节点全部已完成（有正文）时，空的父节点自身成为生成单元。"""
        sections = _tree()
        sections[2].update(status="reviewed", content="正文", word_count=800)   # A1a 完成
        sections[3].update(status="reviewed", content="正文", word_count=800)   # A1b 完成
        got = _ids(select_target_leaves(sections))
        # A1 的子节点都被跳过 → A1 成为叶子；A2、B1 仍为叶子
        assert got == ["A1", "A2", "B1"]

    def test_subtree_scope(self):
        assert _ids(select_target_leaves(_tree(), section_id="A")) == ["A1a", "A1b", "A2"]
        assert _ids(select_target_leaves(_tree(), section_id="A1")) == ["A1a", "A1b"]
        assert _ids(select_target_leaves(_tree(), section_id="B")) == ["B1"]

    def test_subtree_with_skip(self):
        sections = _tree()
        sections[2].update(status="reviewed", content="正文", word_count=900)  # A1a
        assert _ids(select_target_leaves(sections, section_id="A")) == ["A1b", "A2"]

    def test_empty_content_not_skipped(self):
        """有 status 但正文为空/空白 → 仍需生成（避免"空壳已生成"永远跳过）。"""
        sections = _tree()
        sections[2].update(status="generated", content="   ", word_count=0)
        assert "A1a" in _ids(select_target_leaves(sections))


def _sec(sid, title, *, parent="", sort_order=0, description="", content=""):
    return {
        "id": sid, "title": title, "parent_id": parent, "level": 3,
        "sort_order": sort_order, "description": description, "content": content,
    }


class TestBuildSiblingContext:
    """正文生成的同级上下文（标题+描述清单 / 前序正文摘要）。"""

    def _siblings(self):
        return [
            _sec("p", "1.1", parent="A", sort_order=0),
            _sec("a", "1.1.1 支护设计", parent="p", sort_order=1, description="支护形式选型"),
            _sec("b", "1.1.2 开挖工艺", parent="p", sort_order=2, description="分层开挖步骤"),
            _sec("c", "1.1.3 监测方案", parent="p", sort_order=3, description="监测点布置"),
        ]

    def test_list_contains_titles_and_descriptions_excluding_self(self):
        all_sections = self._siblings()
        leaf = all_sections[2]  # 开挖工艺
        lines, _ = build_sibling_context(all_sections, leaf)
        assert "支护设计" in lines and "支护形式选型" in lines
        assert "监测方案" in lines and "监测点布置" in lines
        # 自身不得出现
        assert "开挖工艺" not in lines

    def test_prev_summary_prefers_live_generated_contents(self):
        """前序摘要优先取本次生成过程中的实时正文（旧实现只读启动时 DB 快照 → 恒空）。"""
        all_sections = self._siblings()
        leaf = all_sections[3]  # 监测方案
        live = {"b": "开挖工艺正文……（结尾片段）"}
        _, summary = build_sibling_context(all_sections, leaf, generated_contents=live)
        assert summary == "开挖工艺正文……（结尾片段）"

    def test_prev_summary_falls_back_to_db_content(self):
        all_sections = self._siblings()
        all_sections[2]["content"] = "DB 中已有正文"
        leaf = all_sections[3]
        _, summary = build_sibling_context(all_sections, leaf)
        assert summary == "DB 中已有正文"

    def test_prev_summary_empty_when_no_earlier_section(self):
        all_sections = self._siblings()
        leaf = all_sections[1]  # 第一个同级
        _, summary = build_sibling_context(all_sections, leaf)
        assert summary == ""

    def test_prev_summary_takes_tail_and_truncates(self):
        all_sections = self._siblings()
        all_sections[2]["content"] = "X" * 1000
        leaf = all_sections[3]
        _, summary = build_sibling_context(all_sections, leaf, summary_chars=100)
        assert len(summary) == 100
        assert summary == "X" * 100

    def test_max_siblings_limit(self):
        all_sections = self._siblings()
        all_sections = all_sections + [
            _sec(f"x{i}", f"1.1.{i}", parent="p", sort_order=10 + i) for i in range(10)
        ]
        leaf = _sec("z", "1.1.99", parent="p", sort_order=99)
        all_sections.append(leaf)
        lines, _ = build_sibling_context(all_sections, leaf, max_siblings=2)
        assert len([l for l in lines.splitlines() if l.strip()]) == 2

    def test_prebuilt_index_matches_scan(self):
        """预构建 children_by_parent 索引路径与全表扫描路径结果一致（防 O(N²) 回归）。"""
        all_sections = self._siblings()
        children: dict[str, list] = {}
        for s in all_sections:
            children.setdefault(s.get("parent_id", ""), []).append(s)
        leaf = all_sections[3]
        scanned, s1 = build_sibling_context(all_sections, leaf)
        indexed, s2 = build_sibling_context(
            all_sections, leaf, children_by_parent=children)
        assert indexed == scanned and s1 == s2


class TestWordStatusFor:
    """统一的字数状态口径（正文自动落库 / 手工保存共用）。"""

    def test_under(self):
        assert word_status_for(800, 1500) == "under"    # 800 < 1200

    def test_normal(self):
        assert word_status_for(1500, 1500) == "normal"

    def test_over(self):
        assert word_status_for(2100, 1500) == "over"    # > 1950

    def test_zero_budget_falls_back_to_default(self):
        assert word_status_for(1500, 0) == "normal"
        assert word_status_for(1500, None) == "normal"


class TestTextWordCount:
    """字数口径：图表/代码围栏不计入正文字数（图表与正文一体生成的配套修复）。"""

    def test_plain_text(self):
        assert text_word_count("正文一百字") == len("正文一百字")

    def test_empty_and_none(self):
        assert text_word_count("") == 0
        assert text_word_count(None) == 0

    def test_excludes_mermaid_block(self):
        content = "前文\n```mermaid\nflowchart TD\n  A --> B\n```\n后文"
        assert text_word_count(content) == len("前文\n\n后文")

    def test_excludes_chart_json_block(self):
        content = "说明如下：\n```chart-json\n{\"type\": \"labor\"}\n```\n"
        assert text_word_count(content) == len("说明如下：\n\n")

    def test_excludes_plain_code_block(self):
        content = "示例：\n```python\nprint('x')\n```\n完"
        assert text_word_count(content) == len("示例：\n\n完")

    def test_unclosed_fence_truncates_rest(self):
        # 未闭合围栏 → 从围栏处截断，避免把整段代码当正文
        content = "前文\n```mermaid\nflowchart TD\n  A --> B"
        assert text_word_count(content) == len("前文\n")

    def test_inflation_guard_changes_status(self):
        """回归：一张 mermaid 图不得把 under 的正文抬成 normal。"""
        prose = "甲" * 1000
        chart = ("\n```mermaid\nflowchart TD\n"
                 + "\n".join(f'  N{i}["节点{i}"] --> N{i1}["节点{i1}"]'
                             for i, i1 in zip(range(10), range(1, 11)))
                 + "\n```\n")
        raw = prose + chart
        assert len(raw) >= 1200                       # 旧口径 len(raw) ≥ 1200 → 误判 normal
        assert text_word_count(raw) == 1002           # 仅“甲”*1000 + 两个换行
        assert word_status_for(text_word_count(raw), 1500) == "under"
        assert word_status_for(len(raw), 1500) != "under"
