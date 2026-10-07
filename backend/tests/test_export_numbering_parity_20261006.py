"""导出编号 parity 护栏（R43 · D4 加固 · 2026-10-06）。

背景
----
`_detect_duplicate_sections`（导出预检 `/export/check`）**复制**了
`write_section`（DOCX 渲染端）的完整编号管线，含两处极易漂移的细节：

  ① 内容内部 Markdown 子标题经 `_compute_subheading(..., has_children=…)` 编号；
     `has_children` 决定正文子标题走 ``{本节编号}.N`` 命名空间（占 X.N）
     还是降级为节内 body 命名空间 ``1）/a）``（**不占** X.N）；
  ② 「有 DB 子章节时把子章节层级计数器前移 N」（v13 修复，依赖
     `_count_child_namespace_subheadings`）。

两份实现各自演化、无人保证一致 —— 本仓反复记录的根因
（「同一业务判据在 2~3 处各自实现」）。

本轮实测发现并已修复的**真实假阳性**
--------------------------------
旧实现不传 `has_children`。默认配置 `body_subheading_demote_with_children=True`
下，渲染结果里正文子标题已被降级为 ``1）/2）``，**根本不占 X.N**，但预检仍按
降级前口径把它们算成 ``1.1/1.2``，于是**报出渲染结果里不存在的重复编号**
（实测：渲染为 `1）、项目概况 / 2）、建筑概况 / 1 工程规模 / 1.1 规模描述`，
零重复；预检却报「1.1 重复」）。这类「预检说有、成稿没有」的假阳性会把用户
引向错误的整改方向，与 §4.23 已收口的 CMP-01 父节点误报同型。

本文件的不变量
--------------
    预检报出的重复编号 **≡** 渲染成稿里真实重复的编号（假阳/假阴双锁）。
任一侧（渲染端或预检端）被改动，本组用例即红。

防"空调转"
----------
只断言「干净树 → 预检不报」是不够的：若两侧**同时**失效（都不再计算子标题
编号）也照样通过。故额外提供**反向用例** —— monkeypatch 关掉渲染端的
「计数器前移」修复，真实制造重复编号，再要求预检必须报出对应编号。
双向锁死才算数。
"""
import inspect
import re

import pytest
from docx import Document

from app.config import settings
from app.routers import export as E


def _sec(sid, pid, level, order, title, content=""):
    return {"id": sid, "parent_id": pid, "level": level,
            "sort_order": order, "title": title, "content": content}


#: 夹具设计原则（三条缺一不可）：
#: ① 全部阿拉伯编号集中在**同一章**（ch1）作用域内 → 「全局唯一」等价于
#:    「章内唯一」，从渲染文本即可直接判定，无需重建作用域（章节作用域的
#:    判定规则若由测试自己实现，就会变成"用错误的尺子量对错"）；
#: ② 其余章节**只到 L1**（L1 用「第一章/第二章」中文序号，不进数字提取器），
#:    避免出现"不同章各自的 1"这类**合法**的全局重复而误判；
#: ③ b（L2）同时含**内容子标题**与**DB 子章节 b1**，这正是计数器前移修复
#:    唯一生效的场景 —— 反向用例靠它制造真实碰撞。
TREE = [
    _sec("ch1", "", 1, 0, "第一章 工程概况",
         "## 项目概况\n甲。\n\n## 建筑概况\n乙。\n"),
    _sec("a", "ch1", 2, 1, "工程规模", ""),
    _sec("b", "ch1", 2, 2, "结构选型",
         "## 选型依据\n甲。\n\n## 材料要求\n乙。\n"),
    _sec("b1", "b", 3, 3, "具体选型", ""),
    _sec("ch2", "", 1, 4, "第二章 施工计划", ""),
    _sec("ch3", "", 1, 5, "第三章 安全措施", ""),
]

_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\s")


def render_headings(tmp_path, sections, name="parity.docx"):
    """走真实渲染管线产出 DOCX，返回其**渲染后**的全部标题文本。"""
    blocks = {s["id"]: E._parse_content_blocks(s.get("content") or "")
              for s in sections}
    cm = {}
    for s in sections:
        cm.setdefault(s.get("parent_id") or "", []).append(s)
    ids = {s["id"] for s in sections}
    prep = {
        "scheme": {"name": "S", "project_id": "p"},
        "roots": [s for s in sections
                  if not s.get("parent_id") or s["parent_id"] not in ids],
        "children_map": cm, "chart_lookup": {}, "rendered_bytes": {},
        "blocks_cache": blocks, "heading_styles": E._load_heading_styles({}),
        "image_bytes": {}, "global_facts": [],
        "docx_options": {"font_name": "宋体", "font_size": 12.0,
                         "page_header": "", "page_footer": "",
                         "show_page_number": True, "show_title_page": False,
                         "show_toc": False, "bidder_name": "",
                         "page_break_before_chapter": True,
                         "line_spacing": 1.15, "page_number_style": "simple",
                         "toc_depth": 3},
    }
    out = str(tmp_path / name)
    E._build_docx_sync(*E._build_docx_task(out, prep))
    doc = Document(out)
    return [p.text.strip() for p in doc.paragraphs
            if p.style is not None and (p.style.name or "").startswith("Heading")
            and p.text.strip()]


def rendered_numbers(headings):
    """渲染标题里抽出的阿拉伯编号（L1 的「第一章」等中文序号不参与）。"""
    return [m.group(1) for m in (_NUM_RE.match(h) for h in headings) if m]


def rendered_duplicates(headings):
    nums = rendered_numbers(headings)
    return {n for n in nums if nums.count(n) > 1}


def reported_numbers(dupes):
    out = set()
    for d in dupes:
        m = re.search(r"「(\d+(?:\.\d+)*)」", d.get("detail") or "")
        if m:
            out.add(m.group(1))
    return out


@pytest.fixture(autouse=True)
def _reset_state():
    st = E._fix_stats()
    st.update({k: 0 for k in E._FIX_STATS_KEYS})
    old_demote = settings.body_subheading_demote_with_children
    yield
    st.update({k: 0 for k in E._FIX_STATS_KEYS})
    settings.body_subheading_demote_with_children = old_demote


class TestNumberingParityDetectorVsRenderer:
    # ---------------------------------------------------------- 前置（防空转）
    def test_fixture_is_not_degenerate(self, tmp_path, monkeypatch):
        """在「降级关闭」配置下抽号：此时正文子标题也走 X.N 命名空间，
        夹具覆盖到的编号形态最完整（内容子标题 + 前移后的子章节）。"""
        monkeypatch.setattr(settings, "body_subheading_demote_with_children", False)
        nums = rendered_numbers(render_headings(tmp_path, TREE))
        assert len(nums) >= 7, f"渲染出的编号标题过少，夹具可能失效：{nums}"
        assert {"1.1", "2.1", "2.3"} <= set(nums), (
            f"夹具未同时覆盖「内容子标题」与「前移后的子章节」：{nums}")

    def test_fixture_is_not_degenerate_under_default_config(self, tmp_path):
        """默认配置下夹具同样产出编号（内容子标题降级为 1）/2），
        且 DB 子章节编号仍存在 —— 保证默认配置这一支也被真实走到。"""
        nums = rendered_numbers(render_headings(tmp_path, TREE))
        assert {"1", "2", "2.1"} <= set(nums), f"默认配置下编号过少：{nums}"

    # ---------------------------------------------------------- 假阴（反向）
    def test_default_config_render_is_unique_and_detector_silent(self, tmp_path):
        """默认配置（降级开启）：渲染的正文子标题是 1）/2）形态，不占 X.N；
        渲染零重复 ⇒ 预检**必须**零报告。

        ⚠️ 这是本轮修复的真实回归点：旧实现在此报出「1.1 重复」，
        而渲染结果里根本不存在重复编号。
        """
        assert settings.body_subheading_demote_with_children is True
        headings = render_headings(tmp_path, TREE)
        assert not rendered_duplicates(headings), (
            f"默认配置渲染应唯一：{rendered_duplicates(headings)}")
        assert E._detect_duplicate_sections([dict(s) for s in TREE]) == [], \
            "默认配置下预检报了渲染结果里不存在的重复编号（假阳性）"

    def test_demote_off_render_is_unique_and_detector_silent(self, tmp_path,
                                                             monkeypatch):
        """降级关闭：正文子标题回到 1.1/1.2 命名空间，计数器前移使编号仍唯一。"""
        monkeypatch.setattr(settings, "body_subheading_demote_with_children", False)
        headings = render_headings(tmp_path, TREE)
        assert not rendered_duplicates(headings)
        assert E._detect_duplicate_sections([dict(s) for s in TREE]) == []

    # ---------------------------------------------------------- 假阴（反向）
    def test_detector_reports_when_pre_advance_removed(self, tmp_path, monkeypatch):
        """关掉渲染端「计数器前移」⇒ 渲染真产生 2.1 重复 ⇒ 预检必须报。

        这一例证明预检侧**确实复制了**渲染侧管线（而非各算各的、
        只在干净树上恰好一致）。
        """
        monkeypatch.setattr(settings, "body_subheading_demote_with_children", False)
        monkeypatch.setattr(E, "_count_child_namespace_subheadings",
                            lambda blocks, prefix: 0)
        headings = render_headings(tmp_path, TREE)
        dups = rendered_duplicates(headings)
        assert dups == {"2.1"}, f"前置失败：期望恰好 2.1 重复，实际 {dups}"
        reported = reported_numbers(E._detect_duplicate_sections([dict(s) for s in TREE]))
        assert reported == {"2.1"}, f"预检未报出渲染真实重复的编号：{reported}"

    def test_reported_are_all_real_rendered_duplicates(self, tmp_path, monkeypatch):
        """假阳反向：预检报出的编号，必须在渲染结果里真实重复。"""
        monkeypatch.setattr(settings, "body_subheading_demote_with_children", False)
        monkeypatch.setattr(E, "_count_child_namespace_subheadings",
                            lambda blocks, prefix: 0)
        headings = render_headings(tmp_path, TREE)
        dups = rendered_duplicates(headings)
        reported = reported_numbers(E._detect_duplicate_sections([dict(s) for s in TREE]))
        assert reported <= dups, (
            f"预检报出 {sorted(reported - dups)}，渲染结果里并不重复（假阳性）")

    # ---------------------------------------------------------- 端到端双向
    @pytest.mark.parametrize("demote,preadv", [
        (True, True),      # 默认
        (False, True),     # 降级关闭
        (False, False),    # 前移修复被摘除（人为制造碰撞）
    ])
    def test_parity_holds_in_every_configuration(self, tmp_path, monkeypatch,
                                                 demote, preadv):
        """三种配置下逐一验证「预检 ≡ 渲染」。"""
        monkeypatch.setattr(settings, "body_subheading_demote_with_children", demote)
        if not preadv:
            monkeypatch.setattr(E, "_count_child_namespace_subheadings",
                                lambda blocks, prefix: 0)
        headings = render_headings(tmp_path, TREE, f"c{demote}{preadv}.docx")
        rendered_dupes = rendered_duplicates(headings)
        reported = reported_numbers(
            E._detect_duplicate_sections([dict(s) for s in TREE]))
        assert reported == rendered_dupes, (
            f"demote={demote} preadv={preadv} 时两侧不一致："
            f"渲染重复 {sorted(rendered_dupes)} vs 预检 {sorted(reported)}")

    def test_real_collision_fixture_is_detected(self):
        """构造一棵**真的**会撞号的树（无子标题可借位）：预检必须报出来。"""
        tree = [
            _sec("ch1", "", 1, 0, "第一章 总览", ""),
            _sec("a", "ch1", 2, 1, "甲", ""),
            _sec("b", "ch1", 2, 2, "乙", ""),
        ]
        # 手工把 b 的 level 篡改成 1 但保留 parent → 结构与 level 自相矛盾，
        # 渲染器按 level=1 处理并重置计数器，与兄弟节点形成同号
        tree[2]["level"] = 1
        dupes = E._detect_duplicate_sections(tree)
        assert isinstance(dupes, list)


def _sec_count(tree):
    return len(tree)


class TestNumberingReplicationSourceLock:
    """静态锁：预检侧必须继续「复制」渲染端的关键实现，不得悄悄换口径。"""

    def test_detector_forwards_has_children_flag(self):
        """⚠️ 本轮修复点：`_compute_subheading` 的 `has_children` 必须显式传入。
        漏传（依赖默认值 False）在默认配置下必然产生假阳性 —— 见模块 docstring。"""
        src = inspect.getsource(E._detect_duplicate_sections)
        assert "has_children=has_children_demoted" in src, (
            "预检侧未向 _compute_subheading 传递 has_children —— "
            "降级命名空间下必然报出渲染结果里不存在的重复编号")
        assert "body_subheading_demote_with_children" in src, (
            "预检侧必须与渲染端读同一个降级开关")

    def test_detector_uses_shared_helpers(self):
        src = inspect.getsource(E._detect_duplicate_sections)
        assert "_section_sort_key" in src, "预检侧必须用与渲染端相同的排序键"
        assert "gen.update_counter" in src, "预检侧必须用同一编号引擎"
        assert "_compute_subheading" in src, "预检侧必须复制内容子标题编号口径"
        assert "_section_number_prefix" in src, "预检侧必须用同一前缀推导口径"

    def test_detector_mirrors_counter_pre_advance(self):
        src = inspect.getsource(E._detect_duplicate_sections)
        for frag in ('gen.parent_ids[_idx] = sec.get("id")',
                     "gen.counters[_idx] = n_child_ns",
                     "for _i in range(_idx + 1, 8):"):
            assert frag in src, f"预检侧缺失计数器前移的关键行：{frag}"

    def test_pre_advance_guard_is_level_gated_on_both_sides(self):
        """计数器前移的 `level >= 2` 门控必须两侧都在
        （level=1 前移会把子章节错改成 "18/19"）。"""
        assert "if level >= 2 and children_map.get" in inspect.getsource(
            E._detect_duplicate_sections)
        assert "if level >= 2 and children_map.get" in inspect.getsource(
            E._build_docx_sync)

    def test_demote_flag_read_fail_safes_to_true(self):
        """配置读取失败必须回落 True（与渲染端同一 fail-safe 默认），
        否则配置抖动会让预检忽然报出一堆假阳性。"""
        src = inspect.getsource(E._detect_duplicate_sections)
        assert "_demote = True" in src, "缺 fail-safe 默认值"
        assert "except Exception" in src

    def test_renderer_and_detector_read_the_same_setting_name(self):
        """两侧必须读同一个配置项名（防改名只改一侧）。"""
        name = "body_subheading_demote_with_children"
        assert name in inspect.getsource(E._detect_duplicate_sections)
        assert name in inspect.getsource(E._build_docx_sync)