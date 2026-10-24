"""R131-A · 目录/正文编号一致性专项（P0）· 图号归属漂移修复护栏（2026-10-23）

【根因回顾（探查定案，证据见 file:line）】
图号**章序号**在导出渲染期由 ``_figure_chapter_num(heading_gen)`` 按**当前章节树**
现算（export.py:3726 起），归属链路本身不读缓存；真正的漂移点在**图题侧**：
生成期写进图表载荷 title / 配图 alt 的「图 X-Y」前缀在渲染时被**原样保留**
（修复前 export.py 图表分支与配图分支均不剥号），与现算的新号叠写成
「图 2-1 图 4-1 劳动力配置计划」—— 前半是当前树新号、后半是**过期章号**，
章节移动后即「图号归属漂移 + 重号」（验收标准所指形态）。表侧同型问题已于
2026-10-17 用 ``_strip_leading_table_number`` 收口，图侧一直缺失。

【本轮修复 → 本文件的护栏映射】
  R1 契约模块新增派生判据 ``FIG_TITLE_PREFIX_RE``（由 ``FIG_TITLE_RE`` 派生）→ B1/B2
  R2 export 新增 ``_strip_leading_figure_number`` 并接线到图表 fig_title 与配图 alt → A 组 / B3/B4
  R3 章节移动后图号归属随当前树重算且不重号（渲染期章号现算 + 前缀剥离合流）→ C 组
  R4 跨章借图契约回归护栏（计划 §1.3.4 要求，本轮未改该逻辑，锁契约）→ D 组

分组（每条修复均含 **正向 + 反例**；反例复现**修复前旧错误路径**）：
  A 组 · 图题 / alt 生成期「图 X-Y」前缀剥离（功能级）
  B 组 · 判据唯一事实源 + 渲染接线静态锁（AST / 源码级）
  C 组 · 「章节移动后图号归属漂移」验收用例（正向 = 修复后，反例 = 修复前旧路径）
  D 组 · 跨章借图契约（正向 = 借最近前一张并回传来源；反例 = 本章有登记/无同类型不乱借）

判据纪律：全文件不复制「图{章}-{序}」正则字面量 —— 计数扫描式
``_SCAN_RE`` 同样由 ``FIG_TITLE_PREFIX_RE.pattern`` 派生（唯一事实源）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import app.routers.export as export_mod
from app.routers.export import (
    _EXPORTER_VERSION,
    _figure_chapter_num,
    _resolve_chart_code,
    _strip_leading_figure_number,
)
from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
from app.services.content_format_contract import (
    FIG_TITLE_PREFIX_RE,
    FIG_TITLE_RE,
)

# export.py 源码（AST 静态锁用），模块级只读一次。
_EXPORT_SRC = Path(export_mod.__file__).resolve().read_text(encoding="utf-8")
_EXPORT_LINES = _EXPORT_SRC.splitlines()
_EXPORT_TREE = ast.parse(_EXPORT_SRC)

# ✅ 全文计数扫描式：由唯一事实源去掉行首锚 ``^`` 派生 —— 不是第二份字面量，
#    仅把「必须出现在开头」放宽为「全文任意位置」用于统计图号出现次数。
_SCAN_RE = re.compile(
    FIG_TITLE_PREFIX_RE.pattern[1:] if FIG_TITLE_PREFIX_RE.pattern.startswith("^")
    else FIG_TITLE_PREFIX_RE.pattern)


def _scan_count(text: str) -> int:
    """统计文本里「图{章}-{序}」前缀形态出现次数（判据仍源自契约模块）。"""
    return len(_SCAN_RE.findall(str(text or "")))


def _fn_node(tree: ast.AST, name: str) -> ast.AST:
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"未找到函数 {name}")


# ===========================================================================
# A 组 · 图题 / alt 前缀剥离（功能级：正向 + 反例）
# ===========================================================================
class TestFigurePrefixStrip:

    def test_positive_spec_forms_are_stripped(self):
        """正向：契约认可的图题形态（含无空格形态）一律剥掉生成期前缀。"""
        # 有空格规范形态
        assert _strip_leading_figure_number("图 4-1 劳动力配置计划") == "劳动力配置计划"
        # 无空格不规范形态（派生式更宽，正是 B2 反例锁定的覆盖点）
        assert _strip_leading_figure_number("图4-1机械设备管理流程") == "机械设备管理流程"
        # 破折号变体 + 前后空白
        assert _strip_leading_figure_number("  图 3—2 深基坑监测 ") == "深基坑监测"

    def test_counterexample_old_passthrough_keeps_stale_chapter(self):
        """反例：修复前 title/alt **原样直通**（接线摘除即回到此路径）。

        直通的题注与渲染期现算新号叠写后，成稿同时携带当前树新号与
        过期章号 —— 此断言在修复前形态下恒成立，用于复现旧错误路径。
        """
        raw = "图 4-1 劳动力配置计划"                       # 生成期载荷（旧第 4 章）
        old_caption = f"图 2-1 {raw}"                       # 修复前：fig_title 直通
        new_caption = f"图 2-1 {_strip_leading_figure_number(raw)}"
        # 旧路径：双图号 + 过期章号 4 残留（漂移形态）
        assert _scan_count(old_caption) == 2, "修复前应叠写出「图 2-1 图 4-1 …」双图号"
        assert "图 4-1" in old_caption
        # 修复后：单一图号、无过期章号
        assert new_caption == "图 2-1 劳动力配置计划"
        assert _scan_count(new_caption) == 1
        assert "图 4-1" not in new_caption

    def test_counterexample_non_figure_text_untouched(self):
        """反例：判据不得误伤表题与普通小标题（剥成裸标题即误伤）。"""
        assert _strip_leading_figure_number("表 2-1 材料计划表") == "表 2-1 材料计划表"
        assert _strip_leading_figure_number("编制依据与工程概况") == "编制依据与工程概况"
        assert _strip_leading_figure_number("1.1 施工工艺流程") == "1.1 施工工艺流程"

    def test_failsoft_empty_result_returns_original(self):
        """fail-soft（硬约束）：剥后为空 / 空入参一律返回原文，绝不产出空题、绝不抛。"""
        assert _strip_leading_figure_number("图 4-1") == "图 4-1"   # 无图名 → 保留原文
        assert _strip_leading_figure_number(None) == ""
        assert _strip_leading_figure_number("") == ""


# ===========================================================================
# B 组 · 判据唯一事实源 + 渲染接线静态锁（源码 / AST 级）
# ===========================================================================
class TestSingleSourceAndWiring:

    def test_positive_prefix_derived_from_fig_title_re(self):
        """正向：前缀判据必须由 ``FIG_TITLE_RE`` 派生，且规范图题必匹配前缀式。"""
        assert FIG_TITLE_PREFIX_RE.pattern == \
            FIG_TITLE_RE.pattern[:FIG_TITLE_RE.pattern.rfind(r"\s+")], \
            "FIG_TITLE_PREFIX_RE 不再是 FIG_TITLE_RE 的派生式（两式开始漂移）"
        # 蕴含关系：凡规范图题 ⇒ 必有可剥离的前缀
        for t in ("图 4-1 劳动力配置计划", "图 12-3 施工进度计划", "图 1-1 编制依据"):
            assert FIG_TITLE_RE.match(t) and FIG_TITLE_PREFIX_RE.match(t), t

    def test_counterexample_narrow_derivation_misses_no_space_form(self):
        """反例：若派生退回窄口径（保留尾部 ``\\s+`` 要求），无空格形态漏剥。

        这是本轮修复过程中的真实中间态（初版用 ``rfind(r"\\S")`` 派生），
        此反例锁定「无空格形态必须被覆盖」这一结论不被回退。
        """
        narrow = re.compile(FIG_TITLE_RE.pattern[:FIG_TITLE_RE.pattern.rfind(r"\S")])
        assert narrow.match("图 4-1 劳动力配置计划"), "窄式对有空格形态仍可命中"
        assert not narrow.match("图4-1机械设备管理流程"), "窄式会漏掉无空格形态（=漏剥）"
        # 现行派生式两种形态都命中
        assert FIG_TITLE_PREFIX_RE.match("图4-1机械设备管理流程")

    def test_positive_two_call_sites_inside_write_section(self):
        """正向接线锁：``_strip_leading_figure_number`` 恰有 2 处调用且都在
        ``write_section`` 内，分别覆盖图表 fig_title 与配图 alt 两条分支。"""
        calls = [n for n in ast.walk(_EXPORT_TREE)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_strip_leading_figure_number"]
        assert len(calls) == 2, (
            f"应恰好 2 处调用（图表 fig_title + 配图 alt），实际 {len(calls)}；"
            "若确因新增合法分支而增加，请随分支语义同步更新本护栏并注明依据")
        ws = _fn_node(_EXPORT_TREE, "write_section")
        lo, hi = ws.lineno, ws.end_lineno
        assert all(lo <= c.lineno <= hi for c in calls), "调用点漂移出 write_section"
        call_lines = [c.lineno for c in calls]
        # 分支一：fig_title 赋值
        assert any("fig_title = _strip_leading_figure_number(" in _EXPORT_LINES[i - 1]
                   for i in call_lines), "图表分支 fig_title 未走剥离"
        # 分支二：配图 alt 实参（跨行调用，向后取 3 行窗口）
        assert any('block.get("alt")' in "\n".join(_EXPORT_LINES[i - 1:i + 2])
                   for i in call_lines), "配图分支 alt 未走剥离"

    def test_counterexample_unwired_title_reproduces_drift(self):
        """反例：接线摘除（修复前状态 = 0 处调用）时，渲染期现算号与载荷
        过期号在 ``caption_text = f"图 {figure_num} {title}"``（export.py:4064）
        处叠写 —— 此处用同一拼装式复现旧路径。"""
        fig_num = "2-1"                                        # 当前树现算（第 2 章）
        raw_title = "图 4-1 劳动力配置计划"                    # 生成期载荷，未剥
        assert _scan_count(f"图 {fig_num} {raw_title}") == 2, \
            "未接线时拼装式必然产出双图号（修复前缺陷形态）"

    def test_no_second_figure_anchor_literal_in_export(self):
        """反例锁：export.py 不得出现第二份 ``^图`` 开头的正则字面量副本
        （判据只能来自契约模块 import；表侧既有 ``_LEADING_TABLE_NUM_RE``
        属 R117 存量不在本护栏范围）。"""
        assert 're.compile(r"^图' not in _EXPORT_SRC
        assert 're.compile("^图' not in _EXPORT_SRC
        # 剥离判据的唯一入口是契约模块导入的 FIG_TITLE_PREFIX_RE
        assert "FIG_TITLE_PREFIX_RE" in _EXPORT_SRC
        assert export_mod.FIG_TITLE_PREFIX_RE is FIG_TITLE_PREFIX_RE, \
            "export 侧必须引用契约模块同一对象（非本地重编译副本）"


# ===========================================================================
# C 组 · 「章节移动后图号归属漂移」验收用例（R131-A 验收标准原文）
# ===========================================================================
class TestChapterOwnershipRecalc:

    def _walk_current_tree(self, l1_titles: list[str]):
        """按**当前章节树**顺序逐章走渲染期编号器（与 write_section 同口径）：
        每章 ``format_heading(1, …)`` 推进 L1 计数器后，章号由
        ``_figure_chapter_num`` 现算 —— 不读任何生成期缓存归属。"""
        g = HeadingNumberingGeneratorV2()
        chapter_nums = []
        for title in l1_titles:
            g.format_heading(1, title, "")
            chapter_nums.append(_figure_chapter_num(g))
        return g, chapter_nums

    def test_positive_moved_section_recounted_with_no_duplicate(self):
        """正向：某节生成期在第 4 章（载荷带「图 4-1」），移动到当前树第 2 章后，
        成稿图号 = 「图 2-1」且不与第 4 章现稿图重号。"""
        tree = ["编制依据", "施工部署（原第四章移入）", "质量保证"]
        _g, chapter_nums = self._walk_current_tree(tree)
        assert chapter_nums == [1, 2, 3], "章号必须随当前树序现算"
        # 走到第 2 章（被移动的节）时的现算章号，画两张图（write_section 计数器同口径）
        ch = chapter_nums[1]
        assert ch == 2, "移动后该节图号章序应为 2（非生成期缓存的 4）"
        counters: dict[str, int] = {}
        fig_key = f"ch{ch}"

        def next_caption(raw_title: str) -> str:
            counters[fig_key] = counters.get(fig_key, 0) + 1
            # 拼装式与 export.py:4064 逐字同构；title 侧经真实修复函数剥离
            return f"图 {ch}-{counters[fig_key]} {_strip_leading_figure_number(raw_title)}".strip()

        cap1 = next_caption("图 4-1 劳动力配置计划")   # 载荷携带过期章号
        cap2 = next_caption("施工机械配置")
        assert cap1 == "图 2-1 劳动力配置计划"          # 归属正确
        assert "图 4-1" not in cap1                     # 过期章号已消除
        assert cap2 == "图 2-2 施工机械配置"            # 同章递增
        assert cap1 != cap2, "同章两图不得重号"
        assert _scan_count(cap1) == 1 and _scan_count(cap2) == 1

    def test_counterexample_cached_ownership_drifts_and_duplicates(self):
        """反例（修复前旧路径）：不剥前缀时，移动节成稿携带过期「图 4-1」，
        与当前树第 4 章现稿的「图 4-1」构成**同号两处 = 重号**，
        且单条题注内部即双图号 —— 漂移形态完整复现。"""
        stale_caption = f"图 2-1 {'图 4-1 劳动力配置计划'}"    # 修复前拼装
        assert _scan_count(stale_caption) == 2, "旧路径题注内双图号"
        assert "图 4-1" in stale_caption and "图 2-1" in stale_caption
        # 当前树第 4 章现稿图号（渲染期现算）：同一号码「图 4-1」出现在两章
        ch4_fresh = "图 4-1 原第四章现稿图"
        assert "图 4-1" in stale_caption and "图 4-1" in ch4_fresh, \
            "修复前：移动节携带的过期号与第 4 章现稿号重复"


# ===========================================================================
# D 组 · 跨章借图契约护栏（计划 §1.3.4：正向 + 反例；本轮未改该逻辑）
# ===========================================================================
class TestCrossChapterBorrowContract:

    # 三章文档顺序：sA、sB 各登记一张 flowchart，sC 未登记（类型索引/顺序秩）
    TYPE_INDEX = {"flowchart": [("sA", "codeA"), ("sB", "codeB")]}
    ORDER_RANK = {"sA": 0, "sB": 1, "sC": 2}

    def test_positive_borrows_nearest_previous_with_source(self):
        """正向：本章无同类型登记 → 借**文档顺序上最近的前一张**，并回传来源章节
        （来源供 fallback_borrowed 计数与导出日志上报，D1 可观测性契约）。"""
        code, borrowed = _resolve_chart_code(
            {}, "sC", "flowchart", {}, self.TYPE_INDEX, self.ORDER_RANK)
        assert code == "codeB", "应借最近前一张（sB），非全局第一张"
        assert borrowed == "sB", "借用来源必须回传（零信号 = 旧缺陷）"

    def test_counterexample_own_registration_never_borrows(self):
        """反例：本章 chart_predictions 有登记（真实生产路径走 chart_lookup 命中）
        → 用本章码、``borrowed`` 必须为空（不计入跨章借图）。"""
        code, borrowed = _resolve_chart_code(
            {}, "sA", "flowchart",
            {("sA", "flowchart"): "codeA"},      # 本章登记（= _resolve_chart_code 优先级 2）
            self.TYPE_INDEX, self.ORDER_RANK)
        assert (code, borrowed) == ("codeA", ""), "本章有登记时不得计入跨章借图"

    def test_counterexample_inline_code_and_missing_type(self):
        """反例：块自带内联码优先且不算借用；同类型全库无登记 → 空码空来源
        （宁缺勿乱借，交由渲染侧跳过且不占图号）。"""
        code, borrowed = _resolve_chart_code(
            {"code": "INLINE"}, "sC", "flowchart", {}, self.TYPE_INDEX, self.ORDER_RANK)
        assert (code, borrowed) == ("INLINE", ""), "内联码不得被当成借用"
        assert _resolve_chart_code(
            {}, "sC", "gantt", {}, self.TYPE_INDEX, self.ORDER_RANK) == ("", ""), \
            "无同类型登记时不得乱借"


# ===========================================================================
# 附 · 导出器版本锚（排版逻辑修改必须 +1，v30 → v31，见 export.py:468）
# ===========================================================================
def test_exporter_version_bumped_for_r131():
    """R131-A（2026-10-23）：图题可见文本变化，必须失效旧缓存。
    （逐字断言由 4 个既有版本锚测试覆盖，此处只锁「不低于 31」防止回退。）"""
    assert int(_EXPORTER_VERSION) >= 31, f"导出器版本未随 R131-A 升级: {_EXPORTER_VERSION}"
