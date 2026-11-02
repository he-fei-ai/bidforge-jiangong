"""R149-O · 目录生成模块深度审计收口护栏（2026-10-31）。

本轮修复面（每项独立取证，见 docs/目录生成模块R149深度审计与修复_20261031.md）：

- A 组：**围栏派生标题编号三侧同源**（FIX-A）。R138 只收口了循环体
  （number_body_subheadings），「传哪些块」仍由调用方各自决定 —— 落库侧
  排除围栏派生标题（D-8g 契约），导出预检/写稿两处传全量 ⇒ 围栏标题在
  成稿占号、预览不占号，同一份正文两侧编号错位一格。现三处共用
  ``select_numbering_blocks``，成稿与预览对齐（围栏标题保留原文不占号）。
- B 组：**分步生成「停止」不再丢弃同窗后续单元已完成成果**（FIX-B）。
  外层归位循环遇 stopped 单元即 break，同窗后续单元（gather 已返回、
  tokens 已消耗）的完成结果被静默丢弃 —— 与本函数注释「含本窗各单元
  已完成的章节」及 2026-09-26「单元内不丢已完成」同族矛盾。
- C 组：**目录标题换行清洗**（FIX-C）。全仓唯一换行拦截在 outline_title_fix
  重写出口，AI 生成 / 上传识别 / 目录库 / 手工保存的主写入链路均不清洗，
  ``"工程概况\n（一）编制依据"`` 会原样落库并进 DOCX 标题段。
- D 组：**_save_outline_to_db 批量写行数守卫**（FIX-D）。与 R13/R72 口径
  对齐（「写库只判 None 不判行数」家族）：REST 并发删除使 executemany
  静默少写、无任何日志。
- E 组：**outline Agent baseline 目录跨任务共享**（FIX-E，agent 开关默认关）。
  ``ws.parent / "baseline"`` 恒为固定名 ⇒ 所有任务共享 manifest，restore
  互相污染 / 跨任务清副本致 FileNotFoundError。对齐 content_agent 的
  ``<task_id>_baseline`` 命名。
- F 组：**Agent 初始提示词 vs 工作区输入契约**（FIX-F，agent 开关默认关）。
  requirements 为空时 响应文件要求.md 不落盘，但提示词仍宣称它是
  「唯一依据」⇒ 模型读文件必然 is_error。
- G 组：**stored_id_parts 前导零 / 全角数字 fail-closed**（FIX-G）。
  D-8a 注释声称「含 0 号段一律非法」，实际只拒字面 "0"；全角数字段被
  原样带进编号文本，与前端 outlineNumbering.ts 判据分叉。
- H 组：**审核 / 修复 AI 调用停止响应**（FIX-H）。生成期所有 AI 调用走
  ``_await_with_stats(task_id=...)``（点停止同步取消），审核/补齐/修复/
  标题重写四处是裸 ``asyncio.wait_for(collect_json_response(...))`` ⇒
  停止后仍烧完最长 60+120s 超时预算。
"""
import inspect
import json
import os
import tempfile
import uuid

import app.routers.sse_handlers as sh
import pytest
from app.routers.export import (
    _build_docx_sync,
    _load_heading_styles,
    _section_number_prefix,
)
from app.services.content_blocks import (
    _parse_content_blocks,
    number_body_subheadings,
    select_numbering_blocks,
)
from app.services.numbering import (
    renumber_section_body_subheadings,
)
from docx import Document

from docx_display import display_texts

# ===========================================================================
# A 组：围栏派生标题编号三侧同源（FIX-A）
# ===========================================================================
_FENCED_CONTENT = (
    "## 总体安排\n"
    "正文甲。\n"
    "\n"
    "```markdown\n"
    "## 围栏内补充标题\n"
    "围栏内正文。\n"
    "```\n"
    "\n"
    "## 资源配置\n"
    "正文乙。\n"
)


class TestFenceHeadingNumberingParity:
    def _export_number(self, content: str, prefix: str) -> dict:
        """导出写稿同口径：parse → select → number，返回 {原文本: _fixed_text}。"""
        blocks = _parse_content_blocks(content)
        number_body_subheadings(
            select_numbering_blocks(blocks), section_prefix=prefix,
            section_level=1, sec_id="s1", has_children=False, with_style=True)
        return {b.get("text"): b.get("_fixed_text") for b in blocks
                if b.get("type") == "heading"}

    def test_export_numbers_match_db_side(self):
        """落库侧与导出侧对同一份含围栏标题的正文必须算出相同编号。"""
        stored, changes = renumber_section_body_subheadings(
            _FENCED_CONTENT, "1.2", 1, section_title="工程概况",
            has_db_children=False, section_id="s1")
        assert "2.2 资源配置" in stored, stored
        assert "2.1 总体安排" in stored, stored
        # 导出侧（修复后）：围栏标题不占号 ⇒ 资源配置同为 2.2
        fixed = self._export_number(_FENCED_CONTENT, "2")
        assert fixed["资源配置"] == "2.2 资源配置", fixed
        assert fixed["总体安排"] == "2.1 总体安排", fixed
        # 围栏派生标题不参与编号：无 _fixed_text（成稿保留原文）
        assert fixed.get("围栏内补充标题") is None, fixed

    def test_select_numbering_blocks_semantics(self):
        blocks = _parse_content_blocks(_FENCED_CONTENT)
        picked = select_numbering_blocks(blocks)
        texts = [b.get("text") for b in picked]
        assert texts == ["总体安排", "资源配置"], picked
        assert all(b.get("_from_fence") is None for b in picked)

    def test_docx_fence_heading_unnumbered_and_next_real_heading_not_shifted(
            self):
        """成稿字节：围栏标题保留原文无编号，其后真标题不再被顺移一格。"""
        sections = [{
            "id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
            "title": "第一章 测试章节", "content": _FENCED_CONTENT,
        }]
        blocks = {"c1": _parse_content_blocks(_FENCED_CONTENT)}
        out = os.path.join(tempfile.gettempdir(), "r149_fence.docx")
        _build_docx_sync(
            out, {"id": "s1", "project_id": "p1", "name": "测试方案"},
            sections, {}, {}, {}, blocks,
            "宋体", 12, "", "", False, False, False,
            bidder_name="",
            heading_styles=_load_heading_styles({}),
            page_break_before_chapter=True,
            line_spacing=1.5,
            page_number_style="simple",
            toc_depth=3,
            margins=None, cover_info=None,
        )
        texts = [t for t in display_texts(Document(out)) if t.strip()]
        assert "围栏内补充标题" in texts, texts
        assert not any(t.startswith("1.2 围栏内补充标题") for t in texts), texts
        assert "1.2 资源配置" in texts, texts
        assert not any(t.startswith("1.3 资源配置") for t in texts), texts

    def test_precheck_duplicate_universe_excludes_fence_headings(self):
        """预检判重宇宙与落库侧一致：围栏标题不再被算进编号宇宙。"""
        from app.routers.export import _detect_duplicate_sections
        sections = [{
            "id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
            "title": "第一章 工程概况", "content": _FENCED_CONTENT,
        }, {
            # 真子章节与围栏标题同名：若围栏标题仍占号（1.2），预检会把
            # 「1.2 围栏内补充标题」与子章节「1.2」判成同命名空间冲突。
            "id": "c2", "parent_id": "c1", "sort_order": 1, "level": 2,
            "title": "围栏内补充标题", "content": "",
        }]
        dupes = _detect_duplicate_sections(sections)
        assert dupes == [], dupes

    def test_source_anchor_selector_wired_at_three_sites(self):
        """静态锚：三处调用点都走共享选择器（防回归为传全量）。"""
        import app.routers.export as ex
        import app.services.numbering as nb
        assert inspect.getsource(nb.renumber_section_body_subheadings).count(
            "select_numbering_blocks(") == 1
        ex_src = inspect.getsource(ex)
        assert ex_src.count("select_numbering_blocks(blocks)") == 2, (
            "导出预检 + 写稿两处都必须过滤围栏派生标题")


# ===========================================================================
# C 组：目录标题换行清洗（FIX-C）
# ===========================================================================
class TestTitleNewlineSanitize:
    def test_normalize_outline_folds_newlines_and_keeps_renumber(self):
        """树入口（AI 生成/上传/目录库/save-outline 共用）：换行折空格 + 编号照常。"""
        from app.services.outline_utils import normalize_outline
        tree = [{'title': '工程概况\n（一）编制依据', 'children': [
            {'title': '1.1 施工条件\n说明', 'children': []}]}]
        out = normalize_outline(tree)
        assert out[0]['title'] == '工程概况 （一）编制依据', out[0]['title']
        assert out[0]['id'] == '1'
        assert out[0]['children'][0]['title'] == '施工条件 说明'
        assert out[0]['children'][0]['id'] == '1.1'

    def test_sanitize_idempotent_and_passthrough(self):
        from app.services.numbering import sanitize_outline_title
        assert sanitize_outline_title('甲\n乙') == '甲 乙'
        assert sanitize_outline_title('甲\r\n乙\r\n丙') == '甲 乙 丙'
        assert sanitize_outline_title('普通标题') == '普通标题'
        assert sanitize_outline_title('') == ''
        # 幂等：清洗结果再次清洗零变化
        once = sanitize_outline_title('  甲\n\n乙  ')
        assert sanitize_outline_title(once) == once == '甲 乙'

    async def test_create_and_update_section_sanitize_newline(
            self, db_conn):
        """手工单章两入口与树入口同口径（create_section / update_section）。"""
        from app.models import SectionCreate, SectionUpdate
        from app.routers.sections import create_section, update_section
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES ('p149','测试')")
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES"
            " ('s149','p149','方案R149')")
        await db_conn.commit()
        created = await create_section('s149', SectionCreate(
            title='甲章\n（一）编制依据'), db=db_conn)
        sec_id = created['id']
        cur = await db_conn.execute(
            "SELECT title FROM sections WHERE id=?", (sec_id,))
        assert (await cur.fetchone())['title'] == '甲章 （一）编制依据'
        await update_section('s149', sec_id,
                             SectionUpdate(title='乙章\r\n（二）依据'), db=db_conn)
        cur = await db_conn.execute(
            "SELECT title FROM sections WHERE id=?", (sec_id,))
        assert (await cur.fetchone())['title'] == '乙章 （二）依据'


# ===========================================================================
# D 组：_save_outline_to_db 批量写行数守卫（FIX-D）
# ===========================================================================
async def _seed_p149(db_conn, sid: str = "s149d"):
    await db_conn.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES ('p149d','测试')")
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES"
        " (?, 'p149d', '方案D')", (sid,))
    await db_conn.commit()


async def _seed_section(db_conn, sid: str, sec_id: str, title: str,
                        outline_id: str, level: int = 1, content: str = ""):
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, parent_id,"
        " sort_order, level, status, outline_json, content) VALUES"
        " (?,?,'p149d',?,'',0,?,'empty',?,?)",
        (sec_id, sid, title, level,
         json.dumps({"id": outline_id, "level": level},
                    ensure_ascii=False), content))
    await db_conn.commit()


class TestSaveOutlineRowcountGuard:
    async def test_update_rowcount_mismatch_aborts_409(self, db_conn,
                                                       monkeypatch):
        """批量 UPDATE 少写（并发删除）必须 409 中止，不得静默 ok。"""
        import app.routers.sections as sec_mod
        from app.routers.sections import _save_outline_to_db
        from fastapi import HTTPException
        await _seed_p149(db_conn)
        await _seed_section(db_conn, "s149d", "secA", "甲", "1")
        await _seed_section(db_conn, "s149d", "secB", "乙", "2")
        monkeypatch.setattr(sec_mod, "safe_rowcount",
                            lambda cur, what="": 0)
        with pytest.raises(HTTPException) as ei:
            await _save_outline_to_db(db_conn, "s149d", [
                {"title": "甲改", "children": [], "__original_id": "secA"},
                {"title": "乙", "children": [], "__original_id": "secB"},
            ], source="测试")
        assert ei.value.status_code == 409, ei.value

    async def test_delete_rowcount_mismatch_aborts_409(self, db_conn,
                                                       monkeypatch):
        """级联 DELETE 少删（并发变更）必须 409 中止。"""
        import app.routers.sections as sec_mod
        from app.routers.sections import _save_outline_to_db
        from fastapi import HTTPException
        await _seed_p149(db_conn)
        await _seed_section(db_conn, "s149d", "secA", "甲", "1")
        await _seed_section(db_conn, "s149d", "secB", "乙", "2")

        _real = sec_mod.safe_rowcount

        def _fake(cur, what=""):
            if "级联删除" in what:
                return 0
            return _real(cur, what=what)

        monkeypatch.setattr(sec_mod, "safe_rowcount", _fake)
        with pytest.raises(HTTPException) as ei:
            await _save_outline_to_db(db_conn, "s149d", [
                {"title": "甲", "children": [], "__original_id": "secA"},
            ], source="测试")
        assert ei.value.status_code == 409, ei.value
        # 中止后事务回滚（生产由 get_db 归还连接统一回滚），乙章节仍在
        await db_conn.rollback()
        cur = await db_conn.execute(
            "SELECT COUNT(*) c FROM sections WHERE scheme_id='s149d'")
        assert (await cur.fetchone())["c"] == 2

    async def test_normal_save_unaffected(self, db_conn):
        """无并发时正常保存零影响（守卫不得误伤常规路径）。"""
        from app.routers.sections import _save_outline_to_db
        await _seed_p149(db_conn)
        await _seed_section(db_conn, "s149d", "secA", "甲", "1")
        await _save_outline_to_db(db_conn, "s149d", [
            {"title": "甲改", "children": [], "__original_id": "secA"},
        ], source="测试")
        cur = await db_conn.execute(
            "SELECT title FROM sections WHERE id='secA'")
        assert (await cur.fetchone())["title"] == "甲改"

    def test_source_anchor_three_guards(self):
        """静态锚：三处批量写都必须带 safe_rowcount 行数断言（调用形态锚）。"""
        import inspect

        import app.routers.sections as sec_mod
        src = inspect.getsource(sec_mod._save_outline_to_db)
        assert src.count("safe_rowcount(") == 3, src.count("safe_rowcount(")
        assert "save-outline 批量更新章节" in src
        assert "save-outline 级联删除章节" in src


# ===========================================================================
# I 组：save-as-outline 非 list 载荷守卫（FIX-I）
# ===========================================================================
class TestSaveAsOutlineNonListGuard:
    async def _seed_upload(self, db_conn):
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES ('p149i','测试')")
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES"
            " ('s149i','p149i','方案I')")
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id,"
            " file_name, status) VALUES ('up149i','p149i','s149i','x.md',"
            " 'parsed')")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title,"
            " parent_id, sort_order, level, status, outline_json, content)"
            " VALUES ('secI','s149i','p149i','原有章','',0,1,'empty',?,"
            " '正文。')",
            (json.dumps({"id": "1", "level": 1}, ensure_ascii=False),))
        await db_conn.commit()

    @pytest.mark.parametrize("bad", [{"a": 1}, "abc", 123])
    async def test_non_list_rejected_and_nothing_deleted(
            self, db_conn, bad):
        """非 list 真值必须 400，且不得清空既有 sections（破坏性守卫）。"""
        from app.routers.upload_outline import save_as_outline
        from fastapi import HTTPException
        await self._seed_upload(db_conn)
        with pytest.raises(HTTPException) as ei:
            await save_as_outline("up149i", {"scheme_id": "s149i",
                                             "outline": bad}, db=db_conn)
        assert ei.value.status_code == 400, ei.value
        cur = await db_conn.execute(
            "SELECT COUNT(*) c FROM sections WHERE scheme_id='s149i'")
        assert (await cur.fetchone())["c"] == 1, "既有章节不得被静默清空"


# ===========================================================================
# J 组：save-as-outline 改名 rename_map（FIX-J，R137 BUG-2b 孪生）
# ===========================================================================
class TestSaveAsOutlineRenameSync:
    async def test_rename_keeps_body_without_ghost_heading(
            self, db_conn, monkeypatch):
        """改名保留正文章节不得产生「1.1 旧标题」幽灵子标题。

        场景 = R137 BUG-2b 的 upload 侧孪生：标题匹配命中旧章节后写入
        改写后新标题（真实链路由占位标题自动改写触发），批量重规范化必须
        拿到旧标题（rename_map），否则正文首行旧标题块被编成幽灵子标题。
        """
        import app.routers.upload_outline as upl_mod
        from app.routers.upload_outline import save_as_outline

        await self.__class__._seed_upload_fixture(db_conn)
        # 占位检测必命中（真实链路：占位标题自动改写会改标题）
        monkeypatch.setattr(
            "app.services.outline_quality.detect_generic_titles_for_write",
            lambda outline, context="": [{"title": "总体实施策划"}])
        from app.services.outline_title_fix import autofix_generic_titles_on_save as _unused  # noqa: F401

        async def fake_autofix(outline, hits, **kw):
            for node in outline:
                node["title"] = str(node.get("title") or "") + "改"
            return outline, hits, 1

        monkeypatch.setattr(
            "app.services.outline_title_fix.autofix_generic_titles_on_save",
            fake_autofix)

        result = await save_as_outline(
            "up149j", {"scheme_id": "s149j", "outline": [
                {"title": "总体实施策划", "children": []}]}, db=db_conn)
        assert result.get("ok"), result
        cur = await db_conn.execute(
            "SELECT title, content FROM sections WHERE id='secJ'")
        row = await cur.fetchone()
        assert row["title"] == "总体实施策划改", row["title"]
        content = row["content"] or ""
        assert "1.1 总体实施策划" not in content, content
        assert "总体实施策划改" in content, content
        assert "1.1 子项甲" in content, content

    @staticmethod
    async def _seed_upload_fixture(db_conn):
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES ('p149j','测试')")
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES"
            " ('s149j','p149j','方案J')")
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id,"
            " file_name, status) VALUES ('up149j','p149j','s149j','x.md',"
            " 'parsed')")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title,"
            " parent_id, sort_order, level, status, outline_json, content)"
            " VALUES ('secJ','s149j','p149j','总体实施策划','',0,1,'empty',?,?)",
            (json.dumps({"id": "1", "level": 1}, ensure_ascii=False),
             "## 总体实施策划\n\n正文。\n\n### 子项甲\n\n更多。\n"))
        await db_conn.commit()

    async def test_rename_wiring_source_anchor(self):
        """静态锚：save_as_outline 必须收集 renamed_titles 并传 rename_map。"""
        import inspect

        import app.routers.upload_outline as upl_mod
        src = inspect.getsource(upl_mod.save_as_outline)
        assert "renamed_titles[sid] = _prev_title" in src
        assert "rename_map=renamed_titles" in src


# ===========================================================================
# E/F 组：outline Agent baseline 隔离 + 提示词契约（FIX-E / FIX-F，
#         outline_agent_enabled 默认 False —— 属接线前置修复）
# ===========================================================================
class TestAgentFlowGuards:
    def test_baseline_dir_is_per_task(self):
        """静态锚（调用形态）：baseline 目录必须按任务命名，禁止共享固定名。

        ⚠️ 负向锚只断**完整赋值语句**，不断裸关键字 —— 修复注释里记载了旧
        形态（R144 教训②：锚被自己的注释误伤即假红）。
        """
        import inspect

        import app.services.agent.outline_agent_flow as flow
        src = inspect.getsource(flow.run_outline_agent)
        assert 'ws.parent / f"{task_id}_baseline"' in src
        assert 'baseline_dir = ws.parent / "baseline"' not in src

    def test_instruction_defaults_verbatim(self):
        """默认（has_requirements=True）指示词逐字不变（向后兼容锚）。"""
        from app.services.agent.outline_agent_flow import build_task_instruction
        assert build_task_instruction(has_original_plan=True) == (
            "严格按照响应文件要求.md 组织一级目录，它是目录结构和标题来源的唯一依据。"
            "项目概述.md 仅用于理解背景和术语，不得据此新增一级目录；"
            "原方案.md 仅用于参考标题表达。")
        assert build_task_instruction(has_original_plan=False) == (
            "严格按照响应文件要求.md 组织一级目录，它是目录结构和标题来源的唯一依据。"
            "项目概述.md 仅用于理解背景和术语，不得据此新增一级目录。")

    def test_instruction_without_requirements_matches_seeded_files(self):
        """requirements 为空时指示词不得指向未 seed 的 响应文件要求.md。"""
        from app.services.agent.outline_agent_flow import build_task_instruction, seed_files_from_context
        # 前置事实：requirements 为空 ⇒ 该文件不落盘
        files = seed_files_from_context({"requirements_text": "",
                                         "project_brief": "概述",
                                         "reference_outline": ""})
        assert all(f["path"] != "响应文件要求.md" for f in files)
        text = build_task_instruction(has_original_plan=False,
                                      has_requirements=False)
        assert "唯一依据" not in text, text
        assert "未提供响应文件要求.md" in text
        text2 = build_task_instruction(has_original_plan=True,
                                       has_requirements=False)
        assert "未提供响应文件要求.md" in text2
        assert "原方案.md 仅用于参考标题表达" in text2

    def test_instruction_wiring_source_anchor(self):
        """静态锚：run_outline_agent 必须按 requirements_text 实况传 has_requirements。

        ✅ R154 升级：run_outline_agent 改为薄包装（L-4 工作区收尾清理），
        实际装配在 _run_outline_agent_impl —— 锚随重构指向 impl；包装层锚
        「委托 + 清理」两断言（定义与使用都要锚，R149 教训③）。
        """
        import inspect

        import app.services.agent.outline_agent_flow as flow
        src = inspect.getsource(flow._run_outline_agent_impl)
        assert 'has_requirements = bool(str(context.get("requirements_text")' in src
        assert "has_requirements=has_requirements" in src
        wrapper = inspect.getsource(flow.run_outline_agent)
        assert "await _run_outline_agent_impl(" in wrapper
        assert "cleanup_workspace_and_baseline(" in wrapper


# ===========================================================================
# G 组：stored_id_parts 前导零 / 全角 fail-closed（FIX-G）
# ===========================================================================
class TestStoredIdPartsHardening:
    def test_canonical_ids_unchanged(self):
        """canonical 数据（renumber 唯一产出形态）逐字不变（反向锚）。"""
        from app.services.numbering import _stored_id_parts, stored_id_to_display, stored_id_to_prefix
        assert _stored_id_parts("1") == ["1"]
        assert _stored_id_parts("12.3") == ["12", "3"]
        assert stored_id_to_prefix("1.2") == "2"
        assert stored_id_to_display("3.2.4") == "2.4"
        assert stored_id_to_display("3") == "第三章"

    @pytest.mark.parametrize("bad", ["01.02", "1.00", "00", "１２.３", "1.２"])
    def test_dirty_ids_fail_closed(self, bad):
        """前导零段 / 全角段一律 fail-closed（D-8a 注释自declared口径补齐）。"""
        from app.services.numbering import _stored_id_parts, stored_id_to_prefix
        assert _stored_id_parts(bad) == []
        assert stored_id_to_prefix(bad) == ""

    def test_body_renumber_writes_nothing_for_dirty_id(self):
        """脏编号不得再把 "02"/"３" 前缀写进落库正文（原缺陷的产物形态）。"""
        content = "## 施工条件\n\n正文。\n"
        stored, changes = renumber_section_body_subheadings(
            content, "01.02", 2, section_title="工程概况", section_id="s1")
        assert stored == content, stored
        assert changes == []


# ===========================================================================
# H 组：审核/修复链路停止响应（FIX-H）
# ===========================================================================
class TestReviewStopResponse:
    async def test_entry_gate_skips_when_stopped(self, monkeypatch):
        """停止已置位时审核入口立即返回，不再发起任何 AI 调用。"""
        monkeypatch.setattr(sh, "is_stopped", lambda task_id: True)
        outline, review = await sh._review_and_fix_outline(
            [{"title": "A", "children": []}], "房建", False,
            task_id="t149")

        async def _boom(*a, **k):
            raise AssertionError("停止后不得发起 AI 调用")

        monkeypatch.setattr(sh, "collect_json_response", _boom)
        assert review.get("passed") is True
        assert "已停止" in "".join(review.get("suggestions") or [])

    async def test_stoppable_collect_wires_task_id(self, monkeypatch):
        """包装器必须把 task_id 传给 _await_with_stats（register_child_task 锚点）。"""
        captured: dict = {}

        async def fake_await(coro, push_stats, *args, **kwargs):
            captured["task_id"] = kwargs.get("task_id")
            return await coro

        async def fake_collect(*a, **k):
            return ("ok",)

        monkeypatch.setattr(sh, "_await_with_stats", fake_await)
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        wrapped = sh._stoppable_outline_collect("t149x")
        assert wrapped is not sh.collect_json_response
        assert await wrapped([]) == ("ok",)
        assert captured["task_id"] == "t149x"

    def test_empty_task_id_returns_bare_collect(self):
        """不传 task_id（历史直调方 / 单测）= 裸 collect，行为逐字不变。"""
        assert sh._stoppable_outline_collect("") is sh.collect_json_response

    def test_review_wiring_source_anchor(self):
        """静态锚：审核主调用 / 修复轮 / 三处标题重写 / 补齐 / 两调用点全接线。"""
        import inspect
        review_src = inspect.getsource(sh._review_and_fix_outline)
        # 定义处调用形态锚（R144 教训②：只锚使用点会漏掉「定义被换成裸
        # collect」的变异 —— 包装器在别处仍被测试，链路却已断）
        assert "_collect_ai = _stoppable_outline_collect(task_id)" in review_src
        assert review_src.count("_collect=_collect_ai") == 3
        assert "await asyncio.wait_for(\n                _collect_ai(" in review_src
        assert "return await _collect_ai(" in review_src
        assert "task_id: str = \"\"" in review_src
        assert "if task_id and is_stopped(task_id):" in review_src
        patch_src = inspect.getsource(sh._try_outline_patch)
        assert "task_id: str = \"\"" in patch_src
        assert "_stoppable_outline_collect(task_id)" in patch_src
        gen_src = inspect.getsource(sh.generate_outline)
        assert gen_src.count("task_id=task_id,") >= 2, (
            "两个 _review_and_fix_outline 调用点都必须传 task_id")


# ===========================================================================
# K/L 组：delete_scheme 级联清单对齐（FIX-K）+ buildTree 字段 parity（FIX-L）
# ===========================================================================
class TestSchemeCascadeAndFrontendParity:
    def test_delete_scheme_uses_single_source_list(self):
        """静态锚：delete_scheme 级联清单必须引用 projects._SCHEME_SCOPED_TABLES。"""
        import inspect

        import app.routers.schemes as sm
        src = inspect.getsource(sm.delete_scheme)
        assert "from app.routers.projects import _SCHEME_SCOPED_TABLES" in src
        # 内联字面量清单不得复活（锚完整元组首段，勿用裸关键字——
        # for 循环 `in _SCHEME_SCOPED_TABLES:` 也含冒号）
        assert '"global_facts", "chart_predictions"' not in src
        # 磁盘清理与 projects 同口径（docx+pdf 双扩展名）
        assert 'for _ext in ("docx", "pdf"):' in src

    async def test_delete_scheme_cascades_scan_cache_and_baseline(
            self, db_conn):
        """行为级：单独删方案后 consistency_scan_cache / placeholder_baselines
        与磁盘 .pdf 产物不再残留（旧清单两张表全漏）。"""
        import app.routers.schemes as sm
        from app.config import settings
        sid, pid = "s149k", "p149k"
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, 'K')", (pid,))
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES"
            " (?,?,'方案K')", (sid, pid))
        await db_conn.execute(
            "INSERT OR REPLACE INTO consistency_scan_cache (section_id,"
            " scheme_id, content_hash, context_hash, rows_json) VALUES"
            " ('secK1', ?, 'h', 'c', '[]')", (sid,))
        await db_conn.execute(
            "INSERT OR REPLACE INTO placeholder_baselines (id, scheme_id)"
            " VALUES ('pbK1', ?)", (sid,))
        await db_conn.commit()
        export_dir = getattr(settings, "exports_dir", None)
        if export_dir:
            from pathlib import Path
            stale = Path(str(export_dir)) / f"{sid}_legacy.pdf"
            stale.write_bytes(b"%PDF-1.4 fake")
        result = await sm.delete_scheme(pid, sid, db=db_conn)
        assert result.get("ok"), result
        for table in ("consistency_scan_cache", "placeholder_baselines"):
            cur = await db_conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE scheme_id=?", (sid,))
            assert (await cur.fetchone())["c"] == 0, table
        if export_dir:
            from pathlib import Path
            assert not (Path(str(export_dir)) / f"{sid}_legacy.pdf").exists()

    def test_frontend_buildtree_field_parity(self):
        """跨语言 parity：后端 _build_tree 下发、TreeNode 声明、buildTree 装配
        三面都含 word_status / generation_standard / last_generation_standard。"""
        from pathlib import Path
        page = (Path(__file__).resolve().parents[2]
                / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx")
        if not page.exists():
            pytest.skip("前端源文件缺失，跳过跨语言 parity")
        src = page.read_text(encoding="utf-8")
        fields = ("word_status", "generation_standard",
                  "last_generation_standard")
        # ① 装配侧：buildTree 映射逐字段拷贝
        import re as _re
        m = _re.search(
            r"function buildTree\(nodes: any\[\]\): TreeNode\[\] \{[\s\S]*?\n  \}",
            src)
        assert m is not None, "buildTree 函数源未找到"
        for f in fields:
            assert f"{f}: n.{f}" in m.group(0), f"buildTree 漏拷贝 {f}"
        # ② 类型侧：TreeNode 声明（本页为 `export type TreeNode = {…}`）
        tm = _re.search(r"export type TreeNode = \{[\s\S]*?\n\}", src)
        assert tm is not None, "TreeNode 类型未找到"
        for f in fields:
            assert f"{f}?:" in tm.group(0), f"TreeNode 缺声明 {f}"
        # ③ 下发侧：后端 _build_tree 非全字段分支 SELECT 列
        import inspect as _inspect

        import app.routers.sections as sec_mod
        bsrc = _inspect.getsource(sec_mod._build_tree)
        for f in fields:
            assert f"sec.{f}" in bsrc, f"后端 _build_tree 未下发 {f}"


# ===========================================================================
# B 组：分步生成「停止」不丢弃同窗后续单元已完成成果（FIX-B）
# ===========================================================================
async def _seed_scheme(db_conn, total: int = 4,
                       word_budget: int = 10000) -> str:
    sid, pid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
        (pid, "测试项目"))
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name, word_budget,"
        " config_json) VALUES (?,?,?,?,?)",
        (sid, pid, "长方案-R149", word_budget,
         json.dumps({}, ensure_ascii=False)))
    await db_conn.execute(
        "DELETE FROM sections WHERE scheme_id=?", (sid,))
    await db_conn.commit()
    return sid


def _parse_events(chunks: list) -> list:
    return [json.loads(line[len("data: "):])
            for line in "".join(chunks).splitlines()
            if line.startswith("data: ")]


async def _nop(task_id=None):
    return None


def _patch_ai_4ch(monkeypatch, state):
    """AI 短路：一级 4 章；子目录按章号直出。

    state["stop_before_raise"] 置位时：第 3 章的子目录 AI 先把任务置为
    stopped 再抛异常 —— 单章重试闸门复检 is_stopped 命中 ⇒ 第 3 章返回
    "stopped"（无产出）；第 4 章的调用在此之前已并发返回 ok。
    """
    total = 4

    def fake_render(name: str, **kwargs) -> str:
        if name == "outline_sublevel_system":
            return str(kwargs.get("chapter_id", "?"))
        if name == "outline_sublevel_batch_system":
            return "CHAPTERS:" + ",".join(
                str(c.get("chapter_id", "?")) for c in kwargs.get("chapters") or [])
        return "PROMPT"

    async def fake_collect(messages, validate_fn=None, **kwargs):
        scene = kwargs.get("scene")
        if scene == "outline_level1":
            outline = [{"title": f"第{i}章", "description": f"desc{i}",
                        "children": []} for i in range(1, total + 1)]
            return ({"outline": outline},
                    json.dumps({"outline": outline}, ensure_ascii=False))
        if scene != "outline_sublevel":
            return ({"passed": True, "suggestions": []},
                    json.dumps({"passed": True}, ensure_ascii=False))
        head = messages[0] if messages else ""
        if isinstance(head, list):
            cid = int(str(head[0].get("content", "")))
        else:
            cid = int(str(head).strip())
        if cid == 3 and state.get("stop_ch3_after_ai"):
            state["stopped"] = True          # 模拟「停止落在第 3 章 AI 调用期间」
            raise RuntimeError("模拟网络中断")
        # ✅ R151：每章合规四级子树（3×3×4），旧的「每章 1 个子节叶子」
        # 会被生成后合规终检触发 AI 补齐（桩不可用 → error）
        outline = [
            {"title": f"{cid}.{j}", "description": "子节", "children": [
                {"title": f"{cid}.{j}.{k}", "description": "小节", "children": [
                    {"title": f"{cid}.{j}.{k}.{m}", "description": "细目",
                     "children": []} for m in range(1, 5)]}
                for k in range(1, 4)]}
            for j in range(1, 4)]
        return ({"outline": outline},
                json.dumps({"outline": outline}, ensure_ascii=False))

    monkeypatch.setattr(sh, "render", fake_render)
    monkeypatch.setattr(sh, "collect_json_response", fake_collect)


class TestStopMidWindowKeepsCompletedUnits:
    async def test_completed_unit_after_stopped_unit_is_merged(
            self, db_conn, monkeypatch):
        """同窗内第 3 章 stopped、第 4 章已完成 ⇒ 第 4 章必须进 stopped 产物。

        旧实现：外层归位循环遇第一个 stopped 单元即 break，同窗后续单元
        （gather 已返回、tokens 已消耗）的完成结果被静默丢弃 —— stopped
        产物比实际已完成的少，且无任何 failed 记账。
        """
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
        monkeypatch.setattr(sh, "wait_resume", _nop)
        sid = await _seed_scheme(db_conn)
        state = {"stopped": False, "stop_ch3_after_ai": True}
        _patch_ai_4ch(monkeypatch, state)
        monkeypatch.setattr(sh, "is_stopped",
                            lambda task_id: bool(state["stopped"]))

        resp = await sh.generate_outline(sid, None, db_conn)
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, str)
                          else chunk.decode("utf-8"))
        events = _parse_events(chunks)
        names = [e["event"] for e in events]
        assert names[-1] == "stopped", (names,
                                        [e.get("message") for e in events])
        done = [e for e in events if e["event"] == "stopped"][0]
        titles = [c["title"] for c in done["outline"]]
        assert titles == ["第1章", "第2章", "第4章"], titles
        # 第 3 章确实没有产出（不得凭空补一个空壳章）
        assert "第3章" not in titles

    async def test_no_stop_behavior_unchanged(self, db_conn, monkeypatch):
        """无停止时 4 章全量完成（反向锚：修复不改变正常完成语义）。"""
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
        monkeypatch.setattr(sh, "wait_resume", _nop)
        sid = await _seed_scheme(db_conn)
        state = {"stopped": False, "stop_ch3_after_ai": False}
        _patch_ai_4ch(monkeypatch, state)
        monkeypatch.setattr(sh, "is_stopped",
                            lambda task_id: bool(state["stopped"]))

        resp = await sh.generate_outline(sid, None, db_conn)
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, str)
                          else chunk.decode("utf-8"))
        events = _parse_events(chunks)
        assert events[-1]["event"] == "completed", events[-1]
        titles = [c["title"] for c in events[-1]["outline"]]
        assert titles == ["第1章", "第2章", "第3章", "第4章"], titles
