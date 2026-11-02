"""R137 · 目录生成模块 · 编号幂等与改名同步（2026-10-28）

本轮修复两项「有代码依据 + 有测试结果支撑」的缺陷，本文件为它们的护栏：

A. **E3 降级字母前缀非幂等（P1）**
   ``_compute_subheading`` 在 has_children=True 时，rel≥1 的子标题产出
   ``a、标题``（L7）。该形态不在 ``numbering._STRIP_NUMBER_RE`` 的任何分支内
   （只覆盖 第X章 / （三） / 1） / 点分路径 / 单段数字 / 中文数字），于是落库
   正文的字母前缀永远剥不掉：下一次规范化把它当纯标题再套一层字母 ——
   实测 4 轮逐层膨胀（``a、关键节点`` → ``a、a、关键节点`` → ``a、a、a、关键节点``
   → 四层），且模式切回非降级后残留 ``2.1.1 a、关键节点``。任何结构变更都会跑
   ``_renormalize_all_section_contents``，故这是**持续污染**。
   修法：``_compute_subheading`` 内先经 ``_strip_body_letter_prefix``（有界 8 轮、
   剥空保留原文）—— 该函数是 numbering 落库 / export 预检 / export 写稿三处的
   唯一算法本体，一处收口即三处同源。

B. **改名后旧标题变「幽灵子标题」（P2）**
   ``update_section`` 改名时会触发本章正文重规范化（R89 BUG-N1），但
   ``_strip_duplicate_leading_title`` 按**新**标题比对 ⇒ 正文首行的**旧**标题块
   不再被排除计数 ⇒ 它突然参与编号，被写成 ``1.1 旧标题``（合法外形的幽灵
   子标题），并把其后真子标题整体顺移一格；任一次后续保存都稳定在该错误态。
   修法：新增 ``rename_from``（默认空串 ⇒ 旧行为逐字不变），先把前导重复块
   （按旧标题识别）**改写**为新标题 —— 正文零丢失、不占编号格子，导出端
   ``_strip_duplicate_leading_title(新标题)`` 仍会命中并丢弃该块。
"""
import inspect
import json

from app.models import SectionUpdate
from app.routers.sections import update_section
from app.services.content_blocks import _compute_subheading
from app.services.numbering import renumber_section_body_subheadings

# ============================================================
# 共用样例：1 个 rel=0 + 2 个 rel=1 子标题（复现探针 1 的最小集）
# ============================================================
_SAMPLE = (
    "## 总体安排\n\n正文一。\n\n"
    "### 关键节点\n\n正文二。\n\n"
    "#### 细部构造\n\n正文三。\n\n"
    "### 资源配置\n\n正文四。\n"
)


def _normalize(content, *, has_children, section_title="进度计划",
               section_number="2.1", level=2, rename_from=""):
    return renumber_section_body_subheadings(
        content, section_number, level, section_title,
        has_db_children=has_children, rename_from=rename_from)


# ============================================================
# A 组：E3 降级字母前缀幂等（BUG-1）
# ============================================================
class TestDemotedLetterPrefixIdempotent:
    def test_demote_pass1_then_zero_changes(self):
        """降级模式：第二轮起必须零改动（修复前每轮多叠一层字母）。"""
        first, ch1 = _normalize(_SAMPLE, has_children=True)
        assert ch1, "首轮应发生改写（原始标题无编号）"
        assert "a、关键节点" in first, first
        assert "1）、总体安排" in first, first

        second, ch2 = _normalize(first, has_children=True)
        assert ch2 == [], f"第二轮不得再有改动，实际 {ch2}"
        assert second == first

        third, ch3 = _normalize(second, has_children=True)
        assert ch3 == []
        assert third == first

    def test_no_accumulated_letter_layers(self):
        """多轮之后字母前缀仍只有一层（绝不出现 ``a、a、`` 叠加）。"""
        text = _SAMPLE
        for _ in range(4):
            text, _ = _normalize(text, has_children=True)
        assert "a、a、" not in text, text
        assert text.count("a、关键节点") == 1, text

    def test_switch_demote_to_normal_leaves_no_letter(self):
        """降级 → 非降级切换：不得残留字母前缀（修复前得 ``2.1.1 a、关键节点``）。"""
        demoted, _ = _normalize(_SAMPLE, has_children=True)
        assert "a、关键节点" in demoted
        restored, _ = _normalize(demoted, has_children=False)
        # 非降级模式：字母前缀被剥掉，回落到正常展示态编号（L2~L4 用空格）
        assert "1.1.1 关键节点" in restored, restored
        assert "a、关键节点" not in restored, restored
        assert "a、" not in restored, restored

    def test_self_heals_multi_layer_history(self):
        """已膨胀的历史正文（三层字母）应被自愈为单层。"""
        damaged = (
            "## 1）、总体安排\n\n正文一。\n\n"
            "### a、a、a、关键节点\n\n正文二。\n"
        )
        fixed, _ = _normalize(damaged, has_children=True)
        assert "a、a、" not in fixed, fixed
        assert "### a、关键节点" in fixed, fixed

    def test_strip_does_not_blank_pure_letter_title(self):
        """纯字母前缀标题（仅 ``a、``）不被剥成空标题。"""
        text, style = _compute_subheading("2.1", 2, 2, {}, "a、",
                                          has_children=True)
        # 剥空 ⇒ 保留原文，最终产出仍是「字母前缀 + 原文」而非空串
        assert text, "标题被剥空"
        # ✅ 2026-10-31 层级修正：降级只换命名空间，样式=父级 + 相对深度（2+1）
        assert style == 3


# ============================================================
# B 组：改名同步正文首行旧标题（BUG-2）
# ============================================================
_RENAME_SRC = "## 进度计划\n\n正文。\n\n### 1.1 总控节点\n\n更多。\n"


class TestRenameSyncsLeadingTitle:
    def test_rename_rewrites_leading_block_no_ghost(self):
        """改名后正文首行改写为新标题，不得出现 ``1.1 旧标题`` 幽灵子标题。"""
        out, changes = _normalize(
            _RENAME_SRC, has_children=False, section_number="1", level=1,
            section_title="施工进度计划", rename_from="进度计划")
        assert changes, "改名应产生改写记录"
        assert "## 施工进度计划" in out, out
        assert "1.1 进度计划" not in out, out
        assert "## 进度计划" not in out, out
        # 真子标题编号不被旧标题块占号（仍是 1.1，而非顺移成 1.2）
        assert "### 1.1 总控节点" in out, out

    def test_rename_keeps_all_content(self):
        """改写而非删除：正文段落逐字保留（零丢失）。"""
        out, _ = _normalize(
            _RENAME_SRC, has_children=False, section_number="1", level=1,
            section_title="施工进度计划", rename_from="进度计划")
        assert "正文。" in out and "更多。" in out
        assert out.count("施工进度计划") == 1, out

    def test_rename_is_idempotent(self):
        """同一 rename_from 再跑一次：零改动（改名不是一次性破坏操作）。"""
        once, _ = _normalize(
            _RENAME_SRC, has_children=False, section_number="1", level=1,
            section_title="施工进度计划", rename_from="进度计划")
        twice, changes = _normalize(
            once, has_children=False, section_number="1", level=1,
            section_title="施工进度计划", rename_from="进度计划")
        assert changes == [], changes
        assert twice == once

    def test_default_rename_from_keeps_legacy_behavior(self):
        """``rename_from=""``（默认）⇒ 行为与本轮修复前逐字一致（向后兼容锁）。

        不传旧标题时无法识别「首行是旧标题」，该块会照旧参与编号 —— 这正是
        修复前的行为。锁定它可保证所有非改名调用点零回归。
        """
        out, _ = _normalize(
            _RENAME_SRC, has_children=False, section_number="1", level=1,
            section_title="施工进度计划")
        assert "## 1.1 进度计划" in out, out
        assert "### 1.1.1 总控节点" in out, out

    def test_rename_ignored_when_title_unchanged(self):
        """旧标题 == 新标题时不做任何改写（防误伤）。"""
        same = "## 进度计划\n\n正文。\n"
        out, changes = _normalize(
            same, has_children=False, section_number="1", level=1,
            section_title="进度计划", rename_from="进度计划")
        assert changes == [], changes
        assert out == same


# ============================================================
# C 组：静态锚（防止收口被回退）
# ============================================================
class TestSourceAnchors:
    def test_compute_subheading_strips_letter_prefix(self):
        src = inspect.getsource(_compute_subheading)
        assert "_strip_body_letter_prefix(" in src, src

    def test_renumber_accepts_rename_from(self):
        params = inspect.signature(renumber_section_body_subheadings).parameters
        assert "rename_from" in params, list(params)
        assert params["rename_from"].default == "", params["rename_from"].default

    def test_update_section_reads_title_and_passes_rename_from(self):
        src = inspect.getsource(update_section)
        assert "SELECT id, title FROM sections" in src, src[:400]
        assert "rename_from=_rename_from" in src, "改名链路未透传旧标题"


# ============================================================
# D 组：DB 端到端（update_section 真执行改名链路）
# ============================================================
async def _seed(db, pid="p1", sid="s1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "测试项目"))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "测试方案"))
    await db.commit()


async def _insert_section(db, sid, sec_id, title, level=1, outline_id="1"):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
        "sort_order, level, outline_json) VALUES (?,?,?,?,?,?,?,?)",
        (sec_id, sid, "p1", title, "", 0, level,
         json.dumps({"id": outline_id, "level": level}, ensure_ascii=False)))
    await db.commit()


async def _get_content(db, sec_id):
    cur = await db.execute("SELECT content FROM sections WHERE id=?", (sec_id,))
    row = await cur.fetchone()
    return (row["content"] or "") if row else ""


class TestRenameEndToEnd:
    async def test_rename_patch_leaves_no_ghost_heading(self, db_conn):
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "进度计划")
        await update_section("s1", "sec1", SectionUpdate(
            content="## 进度计划\n\n正文。\n\n### 1.1 总控节点\n\n更多。\n"),
            db=db_conn)
        before = await _get_content(db_conn, "sec1")
        assert "进度计划" in before

        await update_section("s1", "sec1",
                             SectionUpdate(title="施工进度计划"), db=db_conn)
        after = await _get_content(db_conn, "sec1")
        assert "## 1.1 进度计划" not in after, after
        assert "## 进度计划" not in after, after
        assert "## 施工进度计划" in after, after
        assert "正文。" in after and "更多。" in after

    async def test_rename_plus_struct_change_still_synced(self, db_conn):
        """改名 + 结构变更同请求：批量重规范化之外仍须带旧标题补跑一次。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "secA", "A")
        await _insert_section(db_conn, "s1", "secB", "B", outline_id="2")
        await update_section("s1", "secB", SectionUpdate(
            content="## B\n\n正文。\n\n### 2.1 子项\n\n更多。\n"), db=db_conn)
        await update_section("s1", "secB", SectionUpdate(
            title="乙章", sort_order=0), db=db_conn)
        after = await _get_content(db_conn, "secB")
        assert "## B\n" not in after and "## 1.1 B" not in after, after
        assert "## 乙章" in after, after


# ============================================================
# E 组：save-outline 路径改名（BUG-2b，同根因另一个入口）
# ============================================================
class TestSaveOutlineRename:
    async def test_save_outline_rename_syncs_body_title(self, db_conn):
        """目录编辑器改标题后保存（整表重建）不得把旧标题编成幽灵子标题。"""
        from app.routers.sections import _build_tree, _save_outline_to_db

        await _seed(db_conn)
        await _save_outline_to_db(db_conn, "s1",
                                 [{"title": "进度计划", "children": []}],
                                 source="测试")
        sid = (await _build_tree(db_conn, "s1"))[0]["id"]
        await update_section("s1", sid, SectionUpdate(
            content="## 进度计划\n\n正文。\n\n### 总控节点\n\n更多。\n"),
            db=db_conn)
        before = await _get_content(db_conn, sid)
        assert "1.1 总控节点" in before, before

        await _save_outline_to_db(
            db_conn, "s1",
            [{"title": "施工进度计划", "children": [], "__original_id": sid}],
            source="测试")
        after = await _get_content(db_conn, sid)
        assert "## 施工进度计划" in after, after
        assert "1.1 进度计划" not in after, after
        assert "正文。" in after and "更多。" in after

    async def test_renormalize_accepts_rename_map(self):
        from app.routers.sections import _renormalize_all_section_contents

        params = inspect.signature(_renormalize_all_section_contents).parameters
        assert "rename_map" in params, list(params)
        assert params["rename_map"].default is None, params["rename_map"].default
        src = inspect.getsource(_renormalize_all_section_contents)
        assert "rename_from=(rename_map or {}).get(sec_id, \"\")" in src, src
