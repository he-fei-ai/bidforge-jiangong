"""export_check 重复章节编号检测单测（渲染级模拟口径）。

直接验证 ``_detect_duplicate_sections``（无需 DB）。

背景（实测第 8 轮交付文档取证）：DB 存储裸标题、目录树编号干净，但章节**内容
内部**的 Markdown 子标题（``## 项目概况``）与本节 **DB 子章节**在同一 ``X.N``
命名空间各用一套计数器，导出后出现成对的 ``1.1/1.1``。渲染端已通过
「子章节计数器前移」修复（见 export.write_section），本检测器按与渲染端完全
一致的顺序模拟编号，作为回归护栏：
- 内容子标题 + DB 子章节共存 → 前移生效 → 不再报重复；
- 若未来有人破坏渲染端修复 → 重新报出；
- 章自身内容的子标题与其子孙章节的编号碰撞（修复范围外）→ 如实报出。
"""
import pytest

from app.routers.export import _detect_duplicate_sections


def _sec(sid, pid, level, title, content="", sort=0):
    return {"id": sid, "parent_id": pid, "level": level, "title": title,
            "content": content, "sort_order": sort}


class TestDetectDuplicateSections:
    def test_clean_tree_no_duplicates(self):
        secs = [
            _sec("a", "", 1, "第一章 工程概况"),
            _sec("b", "a", 2, "工程基本信息"),
            _sec("c", "b", 3, "工程规模与结构形式"),
            _sec("d", "b", 3, "脚手架搭设部位与高度"),
            _sec("e", "a", 2, "施工环境与地质条件"),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_content_subheadings_with_children_shift_not_duplicate(self):
        """核心回归护栏：内容子标题 + DB 子章节共存时，渲染端前移生效 → 无重复。

        若破坏 write_section 的计数器前移，此处将报出两个 "1.1"。
        """
        secs = [
            _sec("a", "", 1, "第一章 工程概况", "第一章导语。"),
            _sec("p1", "a", 2, "工程基本信息",
                 "本节概述。\n\n## 项目概况\n\n正文。\n\n## 建筑概况\n\n正文。"),
            _sec("s1", "p1", 3, "工程规模与结构形式", "规模正文。"),
            _sec("s2", "p1", 3, "脚手架搭设部位与高度", "部位正文。"),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_chapter_content_subheading_with_l2_child_no_collision(self):
        """章自身内容子标题与 L2 子章节不再误报（2026-09-20 修复口径）。

        章正文子标题（"## 总体安排" → "1.1"）属于 "#.N" 命名空间，而本章 L2
        子章节编号为 "1"/"2"（heading_gen L2 格式），二者本质不构成重复；
        计数器前移只应发生在 L2+ 章节（其正文子标题 "2.1" 恰与 L3 DB 子章节
        同级）。若对一级章也做前移，一级章的 L2 子章节会被错误改成 18/19。
        """
        secs = [
            _sec("a", "", 1, "第一章 工程概况", "## 总体安排\n第一章正文。"),
            _sec("p1", "a", 2, "工程基本信息", "概览正文，无子标题。"),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_mixed_level_subheadings_no_duplicate(self):
        """第 8 轮取证场景：L2 章节正文混用 ###/####/##（AI 层级不齐），
        子标题经「相对深度 + 单调收敛」续排、DB 子章节计数器前移后，
        成稿编号唯一 —— 回归护栏：若有人破坏任意一端，此处将重新报出。"""
        secs = [
            _sec("a", "", 1, "第一章 工程概况", ""),
            _sec("p1", "a", 2, "构造节点与连墙件做法",
                 "### 剪刀撑设置\n### 斜拉杆布置\n## 连墙件应\n"
                 "#### 扣件紧固力矩控制\n### 施工质量控制\n### 安全注意事项"),
            _sec("s1", "p1", 3, "剪刀撑设置要求", ""),
            _sec("s2", "p1", 3, "连墙件布置方式", ""),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_zero_segment_numbering_removed(self):
        """0 段虚编号（"2.1.0.1"）修复：## 回跳后 #### 收敛为单级深入，
        不再在中间插入 0 段。"""
        secs = [
            _sec("a", "", 1, "第一章"),
            _sec("p1", "a", 2, "构造节点与连墙件做法",
                 "### A\n#### B\n## C\n#### D"),
            _sec("s1", "p1", 3, "子节", ""),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_two_chapters_same_local_number_not_flagged(self):
        # 不同章内部都从 1 / 1.1 起算，编号全局不重复
        secs = [
            _sec("a", "", 1, "第一章 工程概况"),
            _sec("b", "a", 2, "工程基本信息"),
            _sec("c", "b", 3, "项目概况"),
            _sec("p", "", 1, "第二章 编制依据"),
            _sec("q", "p", 2, "法律法规"),
            _sec("r", "q", 3, "国家层面"),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_stored_numbered_titles_are_stripped_not_renumbered_twice(self):
        # DB 标题自带编号也不影响（导出器先剥离再重编号）
        secs = [
            _sec("a", "", 1, "第一章 工程概况"),
            _sec("b", "a", 2, "1 工程基本信息"),
            _sec("c", "b", 3, "1.1 项目概况"),
            _sec("d", "b", 3, "1.2 建筑概况"),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_duplicate_chapter_titles_renumbered_not_flagged(self):
        """两个同名章会被编号引擎按位置重排为 第一章/第二章——
        树级字面重复只可能来自内容子标题或损坏的父子链，不来自重名标题。"""
        secs = [
            _sec("a", "", 1, "第一章 工程概况", sort=0),
            _sec("p", "", 1, "第一章 工程概况", sort=1),
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_missing_fields_no_crash(self):
        secs = [
            _sec("b", "a", 3, ""),
            {"id": "x", "parent_id": "a", "level": 3},
        ]
        assert _detect_duplicate_sections(secs) == []

    def test_returns_structured_dicts(self):
        """脏数据（level 与树层级不符）仍如实报出结构化重复条目，
        且字段契约完整（预检端展示依赖 type/section_id/title/detail）。"""
        secs = [
            _sec("a", "", 1, "第一章 工程概况"),
            {"id": "b", "parent_id": "a", "level": 1,
             "title": "仍是一级（脏数据）", "content": "", "sort_order": 1},
        ]
        res = _detect_duplicate_sections(secs)
        assert len(res) >= 1
        for item in res:
            assert set(item.keys()) >= {"type", "section_id", "title", "detail"}
# ============================================================
# _compute_subheading 相对深度编号（2026-09-20 修复的单测兜底）
# ============================================================
from app.routers.export import _compute_subheading


class TestComputeSubheadingMixedLevels:
    """正文子标题按「相对深度」编号：回跳续接兄弟序列、深跳收敛不产 0 段。"""

    def test_backward_jump_continues_sibling_sequence(self):
        # 章节 "1 构造节点与连墙件做法"，旧实现：回跳的 ## 重启为 1.1（重复）
        c = {}
        t1, s1 = _compute_subheading("1", 2, 3, c, "剪刀撑设置")
        t2, s2 = _compute_subheading("1", 2, 4, c, "扣件紧固力矩控制")
        t3, s3 = _compute_subheading("1", 2, 2, c, "连墙件应")     # 回跳到 ##
        t4, s4 = _compute_subheading("1", 2, 3, c, "施工质量控制")  # 回到 ###
        assert t1 == "1.1 剪刀撑设置"
        assert t2 == "1.1.1 扣件紧固力矩控制"
        assert t3 == "1.2 连墙件应"          # 旧实现得 "1.1"（与 t1 重复）
        assert t4 == "1.3 施工质量控制"      # 旧实现得 "1.1.1"（与 t2 重复）
        assert (s1, s2, s3, s4) == (3, 4, 3, 3)

    def test_deep_jump_no_zero_segment(self):
        # 旧实现：## 后直接 #### 得 "1.1.0.1"（0 段虚编号）
        c = {}
        t1, _ = _compute_subheading("2", 2, 3, c, "A")
        t2, s2 = _compute_subheading("2", 2, 5, c, "B")
        t3, _ = _compute_subheading("2", 2, 3, c, "C")
        assert t1 == "2.1 A"
        assert t2 == "2.1.1 B"     # 旧实现得 "2.1.0.1"
        assert t3 == "2.2 C"
        assert s2 == 4

    def test_purely_deep_first_heading_starts_at_one(self):
        # 首个子标题就是 #### → 从 .1 起算（不因原始层级产生空深）
        c = {}
        t1, s1 = _compute_subheading("1", 2, 4, c, "深层A")
        t2, _ = _compute_subheading("1", 2, 5, c, "更深B")
        assert t1 == "1.1 深层A"
        assert t2 == "1.1.1 更深B"
        assert s1 == 3

    def test_empty_pure_still_advances_counter(self):
        # 空标题（纯 `##` / `###` 残行）不计入正文，但序号不可跳跃留空
        c = {}
        _t, _ = _compute_subheading("1", 2, 2, c, "")
        t2, _ = _compute_subheading("1", 2, 2, c, "有效标题")
        assert t2 == "1.2 有效标题"  # 从 1.2 起（1.1 已被空标题占位）
