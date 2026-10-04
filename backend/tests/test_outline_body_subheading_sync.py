"""目录生成 · 正文子标题编号统一命名空间（2026-09-26 补齐死代码）

背景：services/numbering.renumber_section_body_subheadings 早已写好却长期零生产调用，
导致正文 Markdown 子标题编号只在「导出时」重算，落库正文保留 AI 写的存储态编号
（如 "3.2.1"），出现两类不一致：
  1) 前端预览 ≠ 落库正文 ≠ 导出成稿（三处不同源）；
  2) 本节存在 DB 子章节时，正文子标题与 DB 子章节共用同一编号空间 → 撞号
     （「1.1 正文标题」与「1.1 DB 子章节」成对出现）。

现将该函数在两条正文落库路径接线：
  - sections.update_section（手动保存正文）；
  - sse_handlers._persist_section（AI 正文生成落库，与手动保存同口径）。

测试覆盖：
  - 无 DB 子章节：存储态编号 "3.2" → 正文子标题折算为 "2.1 / 2.1.1"（与导出口径一致）；
  - 有 DB 子章节：一级章 "1" → 正文子标题降级为节内 body 命名空间 "1）、…"，隔离冲突；
  - 幂等：已规范化正文再次保存零改动、不抛异常；
  - 容错：章节编号非法（UUID/空）时原样返回，不阻断保存；
  - 三处同源：落库正文与 export 同算法折算结果一致（复用 numbering 公共实现）。
"""
import json

import pytest
from app.models import SectionUpdate
from app.routers.sections import update_section


async def _seed(db, pid="p1", sid="s1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "测试项目"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        (sid, pid, "测试方案"))
    await db.commit()


async def _insert_section(db, sid, sec_id, title, parent_id="", level=1,
                          outline_id=""):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
        "sort_order, level, outline_json) VALUES (?,?,?,?,?,?,?,?)",
        (sec_id, sid, "p1", title, parent_id, 0, level,
         json.dumps({"id": outline_id, "level": level}, ensure_ascii=False)
         if outline_id else ""))


async def _get_content(db, sec_id):
    cur = await db.execute("SELECT content FROM sections WHERE id=?", (sec_id,))
    row = await cur.fetchone()
    return row["content"] if row else None


async def _child_count(db, sid, sec_id):
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM sections WHERE parent_id=? AND scheme_id=?",
        (sec_id, sid))
    return (await cur.fetchone())["n"]


# ============================================================
# 无 DB 子章节：存储态 → 展示态折算
# ============================================================
class TestBodySubheadingNoChildren:
    async def test_normalizes_storage_id_to_display_prefix(self, db_conn):
        """存储态 "3.2" 的章节，正文子标题应折算为 "2.1 / 2.1.1"（与导出同口径）。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        content = (
            "## 3.2.1 总体安排\n\n正文一。\n\n"
            "### 3.2.1.1 关键节点\n\n正文二。\n"
        )
        await update_section("s1", "sec32", SectionUpdate(content=content),
                              db=db_conn)
        saved = await _get_content(db_conn, "sec32")
        assert "## 2.1 总体安排" in saved, saved
        assert "### 2.1.1 关键节点" in saved, saved
        # AI 写的存储态错误编号已消失
        assert "3.2.1" not in saved, saved

    async def test_deep_heading_relative_depth(self, db_conn):
        """更深层级子标题（###）按相对深度折算为 "2.1.1"。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        content = "## 3.2.1 总体安排\n\n### 3.2.1.1 关键节点\n\n内容。\n"
        await update_section("s1", "sec32", SectionUpdate(content=content),
                              db=db_conn)
        saved = await _get_content(db_conn, "sec32")
        assert "## 2.1 总体安排" in saved
        assert "### 2.1.1 关键节点" in saved


# ============================================================
# 有 DB 子章节：降级为节内 body 命名空间，隔离冲突
# ============================================================
class TestBodySubheadingWithDbChildren:
    async def test_demotes_to_body_namespace_when_has_children(self, db_conn):
        """一节存在 DB 子章节时，正文子标题降级为 "1）、…"，不再与 DB 子章节撞号。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "施工工艺", level=1,
                              outline_id="1")
        # 该章有一个 DB 子章节（编号 1.1），模拟「DB 子章节与正文子标题共存」
        await _insert_section(db_conn, "s1", "sec11", "施工准备", parent_id="sec1",
                              level=2, outline_id="1.1")
        await db_conn.commit()
        assert await _child_count(db_conn, "s1", "sec1") == 1
        content = "## 1.1 工艺概述\n\n正文。\n"
        await update_section("s1", "sec1", SectionUpdate(content=content),
                              db=db_conn)
        saved = await _get_content(db_conn, "sec1")
        # 降级为节内 body 命名空间（数字+）），与 DB 子章节（1.1）彻底隔离
        assert "## 1）、工艺概述" in saved, saved
        assert "1.1 工艺概述" not in saved, saved

    async def test_no_demote_when_no_db_children(self, db_conn):
        """无 DB 子章节时仍走正常展示态折算（不降级）。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "施工工艺", level=1,
                              outline_id="1")
        await db_conn.commit()
        assert await _child_count(db_conn, "s1", "sec1") == 0
        content = "## 1.1 工艺概述\n\n正文。\n"
        await update_section("s1", "sec1", SectionUpdate(content=content),
                              db=db_conn)
        saved = await _get_content(db_conn, "sec1")
        # 无 DB 子章节：一级章正文子标题为 "1.1"（正常展示态），非 "1）"
        assert "## 1.1 工艺概述" in saved, saved


# ============================================================
# 幂等 / 容错
# ============================================================
class TestBodySubheadingIdempotentAndRobust:
    async def test_idempotent_on_already_normalized(self, db_conn):
        """已规范化正文再次保存零改动（幂等，不抛异常）。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        normalized = "## 2.1 总体安排\n\n正文。\n"
        await update_section("s1", "sec32", SectionUpdate(content=normalized),
                              db=db_conn)
        saved1 = await _get_content(db_conn, "sec32")
        # 再存一次
        await update_section("s1", "sec32", SectionUpdate(content=saved1),
                              db=db_conn)
        saved2 = await _get_content(db_conn, "sec32")
        assert saved1 == saved2

    async def test_invalid_stored_id_returns_unchanged(self, db_conn):
        """章节编号非法（空 outline_json）时原样返回，不丢内容、不抛异常。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "secX", "临时章", level=1,
                              outline_id="")
        await db_conn.commit()
        content = "## 1.1 工艺概述\n\n正文。\n"
        await update_section("s1", "secX", SectionUpdate(content=content),
                              db=db_conn)
        saved = await _get_content(db_conn, "secX")
        assert saved == content, saved


# ============================================================
# 2026-09-26 修复批次：接线时序 + 开关生效
# ------------------------------------------------------------
# F1 时序：update_section 把编号规范化放在 fields UPDATE 之前 —— 同一次请求
#          既改 parent_id 又改正文时，规范化读到移动**前**的编号，
#          随后 renumber_sections_after_reorder 改号 → 正文子标题永久错位；
# F2 开关：content_subheading_renumber 在两条落库路径都没被读取，
#          配置项形同虚设（文档承诺「设为 False 回退旧行为」实际无效）。
# ============================================================


class TestRenumberHappensAfterStructRenumber:
    async def test_move_and_edit_content_uses_new_numbering(self, db_conn):
        """同一次请求改 parent_id + content：正文子标题按**移动后**的编号折算。

        旧实现（F1）先按旧编号 "3" 规范化出 "3.1 …"，随后重排把本节变成
        "1.1"，正文停留在 "3.1" —— 与目录、导出永久错位。
        """
        import json as _json
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "工程概况", level=1,
                              outline_id="1")
        await _insert_section(db_conn, "s1", "sec3", "施工部署", level=1,
                              outline_id="3")
        await db_conn.commit()
        await update_section("s1", "sec3", SectionUpdate(
            content="## 总体安排\n\n正文。\n", parent_id="sec1"), db=db_conn)
        saved = await _get_content(db_conn, "sec3")
        cur = await db_conn.execute(
            "SELECT outline_json FROM sections WHERE id='sec3'")
        num = _json.loads((await cur.fetchone())["outline_json"])["id"]
        assert num == "1.1", num
        assert "## 1.1 总体安排" in saved, saved
        assert "3.1" not in saved, saved

    async def test_pure_content_edit_still_normalizes(self, db_conn):
        """仅改正文（无结构变更）时规范化照常生效（不因调整时序而回退）。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        await update_section("s1", "sec32", SectionUpdate(
            content="## 3.2.1 总体安排\n\n正文。\n"), db=db_conn)
        assert "## 2.1 总体安排" in await _get_content(db_conn, "sec32")


class TestRenumberConfigSwitch:
    async def test_switch_off_keeps_original(self, db_conn, monkeypatch):
        """content_subheading_renumber=False → 原样保留 AI 编号（回退旧行为）。"""
        from app.config import settings
        monkeypatch.setattr(settings, "content_subheading_renumber", False)
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        content = "## 3.2.1 总体安排\n\n正文。\n"
        await update_section("s1", "sec32", SectionUpdate(content=content),
                             db=db_conn)
        assert await _get_content(db_conn, "sec32") == content

    async def test_switch_on_normalizes(self, db_conn, monkeypatch):
        """content_subheading_renumber=True → 规范化生效（默认值语义）。"""
        from app.config import settings
        monkeypatch.setattr(settings, "content_subheading_renumber", True)
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        await update_section("s1", "sec32", SectionUpdate(
            content="## 3.2.1 总体安排\n\n正文。\n"), db=db_conn)
        assert "## 2.1 总体安排" in await _get_content(db_conn, "sec32")

    async def test_code_fence_untouched(self, db_conn):
        """非图表代码围栏（如 ```python）不被编号规范化触碰。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        content = (
            "## 3.2.1 总体安排\n\n"
            "```python\nx = 1 + 2\n```\n"
            "### 3.2.1.1 关键节点\n\n内容。\n"
        )
        await update_section("s1", "sec32", SectionUpdate(content=content),
                              db=db_conn)
        saved = await _get_content(db_conn, "sec32")
        assert "## 2.1 总体安排" in saved
        assert "### 2.1.1 关键节点" in saved
        # 普通代码围栏保持原样（编号规范化只重写 # 标题行）
        assert "x = 1 + 2" in saved


class TestNormalizeHelperContract:
    """normalize_section_content_subheadings 的边界契约（防回归）"""

    async def test_missing_section_returns_unchanged(self, db_conn):
        """章节不存在 → 原样返回，不抛异常（不阻断落库）。"""
        from app.services.numbering import normalize_section_content_subheadings
        await _seed(db_conn)
        out, changed = await normalize_section_content_subheadings(
            db_conn, "s1", "nope", "## 1.1 x\n")
        assert changed is False and out == "## 1.1 x\n"

    async def test_other_scheme_section_not_leaked(self, db_conn):
        """跨方案同 id 章节不可被读取（防按别方案编号做规范化）。"""
        from app.services.numbering import normalize_section_content_subheadings
        await _seed(db_conn, pid="p1", sid="s1")
        await _seed(db_conn, pid="p1", sid="s2")
        await _insert_section(db_conn, "s2", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        content = "## 3.2.1 总体安排\n"
        out, changed = await normalize_section_content_subheadings(
            db_conn, "s1", "sec32", content)
        assert changed is False and out == content

    async def test_empty_content_short_circuit(self, db_conn):
        """空正文直接短路（不产生无谓 DB 往返）。"""
        from app.services.numbering import normalize_section_content_subheadings
        out, changed = await normalize_section_content_subheadings(
            db_conn, "s1", "x", "")
        assert out == "" and changed is False

    async def test_word_count_resynced_after_renumber(self, db_conn):
        """规范化改写正文后，字数字段必须与新正文一致（不回写旧字数）。"""
        from app.services.content_utils import text_word_count
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        await update_section("s1", "sec32", SectionUpdate(
            content="## 3.2.1 总体安排\n\n" + "内容" * 200 + "\n"), db=db_conn)
        cur = await db_conn.execute(
            "SELECT content, word_count FROM sections WHERE id='sec32'")
        row = await cur.fetchone()
        assert row["word_count"] == text_word_count(row["content"])
        assert "3.2.1" not in row["content"]

    async def test_content_only_edit_keeps_scan_cache(self, db_conn):
        """仅改正文（无结构变更）不得整方案清空一致性扫描缓存（避免过度失效）。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec32", "进度计划", level=2,
                              outline_id="3.2")
        await db_conn.commit()
        await db_conn.execute(
            "INSERT OR REPLACE INTO consistency_scan_cache"
            " (section_id, scheme_id, content_hash, context_hash, rows_json)"
            " VALUES ('sec32','s1','h1','c1','[]')")
        await db_conn.commit()
        # 正文无子标题 → 编号规范化不改写，但仍是一次「只改正文」的保存
        await update_section("s1", "sec32", SectionUpdate(content="新正文内容"),
                             db=db_conn)
        cur = await db_conn.execute(
            "SELECT COUNT(*) AS n FROM consistency_scan_cache WHERE scheme_id='s1'")
        assert (await cur.fetchone())["n"] == 1
