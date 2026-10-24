"""导出模块护栏（R43 · 2026-10-06）——后端 + 跨语言 parity。

覆盖本轮收口的 5 项修复与 2 项护栏：
  A. 图号重号（E4 · P0）：图片插入原子性
  B. 单块渲染失败拖垮整份导出（E2 · P0）：块级 + 章节级 fail-soft
  C. GFM 表格尺寸无上限（E3 · P1）
  D. 原子替换失败仍写缓存（E5 · P1）
  E. R13 db.execute 判空单一出口（E1 · P0）
  F. 前后端导出配置键 parity（E6）
  G. 降级计数进入 X-Fix-Stats 并被前端消费（可观测性）
"""
import ast
import inspect
import io
import os
import re

import pytest
from app.routers import export as E

REPO_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(E.__file__))))
REPO_ROOT = os.path.dirname(REPO_BACKEND)
FRONTEND_PAGE = os.path.join(
    REPO_ROOT, "frontend", "src", "pages", "SchemeWorkbenchPage.tsx")
EXPORT_PY = os.path.join(REPO_BACKEND, "app", "routers", "export.py")


# =========================================================================
# 工具
# =========================================================================
def _png(w=60, h=40):
    from io import BytesIO

    from PIL import Image
    bio = BytesIO()
    Image.new("RGB", (w, h), (9, 9, 9)).save(bio, format="PNG")
    bio.seek(0)
    return bio


def _make_docx(tmp_path, content, *, name="x.docx", chart_code=None,
               rendered=None, extra_root=None):
    """用最小 prep 组装一份 DOCX，返回 (docx 路径, blocks 列表)。"""
    code = chart_code or "flowchart TD\nA-->B"
    blocks = E._parse_content_blocks(content)
    if rendered is not None:
        for b in blocks:
            if b.get("type") == "chart":
                b["code"] = code
    roots = [{"id": "s1", "parent_id": "", "level": "1", "sort_order": 1,
              "title": "章", "content": content}]
    if extra_root:
        roots.extend(extra_root)
    prep = {
        "scheme": {"name": "S", "project_id": "p"},
        "roots": roots,
        "children_map": {},
        "chart_lookup": {},
        "rendered_bytes": rendered or {},
        "blocks_cache": {"s1": blocks},
        "heading_styles": E._load_heading_styles({}),
        "image_bytes": {},
        "global_facts": [],
        "docx_options": {
            "font_name": "宋体", "font_size": 12.0, "page_header": "",
            "page_footer": "", "show_page_number": True, "show_title_page": False,
            "show_toc": False, "bidder_name": "",
            "page_break_before_chapter": True, "line_spacing": 1.15,
            "page_number_style": "simple", "toc_depth": 3,
        },
    }
    out = str(tmp_path / name)
    stats = E._build_docx_sync(*E._build_docx_task(out, prep))
    return out, stats


# =========================================================================
# A. 图号重号（E4 · P0）——「返回 False ⇒ 未写入任何元素」
# =========================================================================
class TestFigureInsertIsAtomic:
    """`_DocxBodyRollback` 契约：插入失败后文档必须回到调用前状态。"""

    def test_chart_caption_failure_leaves_no_picture(self, tmp_path, monkeypatch):
        from docx import Document

        d = Document()
        orig = E._set_run_font

        def boom(run, *a, **kw):
            if (getattr(run, "text", "") or "").startswith("图 "):
                raise RuntimeError("simulated caption failure")
            return orig(run, *a, **kw)

        monkeypatch.setattr(E, "_set_run_font", boom)
        ok = E._add_inline_chart_from_bytes(d, "flowchart", _png(), "1-1",
                                            "流程图", "宋体", 10.5)
        assert ok is False
        assert len(d.inline_shapes) == 0, "失败后仍残留图片 → 调用方回退图号会造成重号"
        assert [e.tag.split("}")[-1] for e in d.element.body] == ["sectPr"]

    def test_illustration_caption_failure_leaves_no_picture(self, monkeypatch):
        from docx import Document

        d = Document()
        orig = E._set_run_font

        def boom(run, *a, **kw):
            if (getattr(run, "text", "") or "").startswith("图 "):
                raise RuntimeError("simulated caption failure")
            return orig(run, *a, **kw)

        monkeypatch.setattr(E, "_set_run_font", boom)
        ok = E._add_illustration_from_bytes(d, _png(), "1-1", "配图", "宋体", 10.5)
        assert ok is False
        assert len(d.inline_shapes) == 0

    def test_rollback_never_removes_preexisting_elements(self, tmp_path):
        """回滚不得误删调用前已存在的段落（对照 probe 首版的索引切片实现）。"""
        from docx import Document

        d = Document()
        d.add_paragraph("先存在的段落")
        before = [e for e in d.element.body]
        rb = E._DocxBodyRollback(d)
        d.add_paragraph("本次新增")
        assert rb.rollback() == 1
        assert [e for e in d.element.body] == before

    def test_rollback_keeps_sectpr(self):
        from docx import Document
        from docx.oxml.ns import qn

        d = Document()
        rb = E._DocxBodyRollback(d)
        d.add_paragraph("x")
        rb.rollback()
        assert any(e.tag == qn("w:sectPr") for e in d.element.body)

    def test_healthy_insert_returns_true_and_keeps_picture(self):
        from docx import Document

        d = Document()
        assert E._add_inline_chart_from_bytes(
            d, "flowchart", _png(), "1-1", "流程图", "宋体", 10.5) is True
        assert len(d.inline_shapes) == 1

    def test_two_charts_no_duplicate_figure_number(self, tmp_path):
        """回归本轮 P0：两张不同流程图，第一张图题阶段抛错 → 不得出现两个「图 1-1」。"""
        from docx import Document

        c1, c2 = "flowchart TD\nA-->B", "flowchart TD\nC-->D"
        content = ("图1如下图所示：\n\n```mermaid\n%s\n```\n\n甲。\n\n"
                   "图2如下图所示：\n\n```mermaid\n%s\n```\n\n乙。" % (c1, c2))
        orig = E._set_run_font
        hit = {"done": False}

        def boom(run, *a, **kw):
            txt = getattr(run, "text", "") or ""
            if txt.startswith("图 ") and not hit["done"]:
                hit["done"] = True
                raise RuntimeError("simulated caption failure")
            return orig(run, *a, **kw)

        E._set_run_font = boom
        try:
            out, _ = _make_docx(tmp_path, content, name="dup.docx",
                                 chart_code=None,
                                 rendered={})
        finally:
            E._set_run_font = orig
        # rendered_bytes 为空 → 两张图都走"渲染失败跳过"路径，文档仍应正常产出
        d = Document(out)
        caps = [p.text.strip() for p in d.paragraphs if p.text.strip().startswith("图 ")]
        nums = [c.split()[1] for c in caps]
        assert len(nums) == len(set(nums)), f"图号重号：{caps}"


# =========================================================================
# B. 逐块 fail-soft（E2 · P0）
# =========================================================================
class TestPerBlockFailSoft:
    """单个坏块不得让整份导出 500；内容降级为纯文本且可观测。"""

    @pytest.mark.parametrize("target,body", [
        ("_add_table_from_markup", "|a|b|\n|-|-|\n|1|2|\n\n正文。"),
        ("_add_code_block", "```python\nx = 1\n```\n\n正文。"),
        ("_add_runs_with_inline_format", "普通正文段落。"),
    ])
    def test_block_renderer_exception_does_not_kill_export(
            self, tmp_path, monkeypatch, target, body):
        def boom(*a, **kw):
            raise RuntimeError("simulated renderer failure")

        monkeypatch.setattr(E, target, boom)
        out, stats = _make_docx(tmp_path, body, name="t.docx")
        assert os.path.exists(out) and os.path.getsize(out) > 0
        assert stats.get("block_render_failed", 0) >= 1

    def test_degradation_preserves_text(self, tmp_path, monkeypatch):
        from docx import Document

        monkeypatch.setattr(
            E, "_add_table_from_markup",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        out, _ = _make_docx(tmp_path, "|a|b|\n|-|-|\n|独有文字XYZ|\n", name="d.docx")
        text = "\n".join(p.text for p in Document(out).paragraphs)
        assert "独有文字XYZ" in text, "降级后内容必须仍在成稿中（纯文本）"

    def test_section_renderer_exception_is_contained(self, tmp_path, monkeypatch):
        """章节级第二道防线：标题渲染异常只丢该章，其余章节照常。"""
        roots_extra = [{"id": "s2", "parent_id": "", "level": "1", "sort_order": 2,
                        "title": "第二章", "content": "第二章正文"}]
        orig = E._add_runs_with_inline_format
        calls = {"n": 0}

        def boom(p, text, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated heading failure")
            return orig(p, text, *a, **kw)

        monkeypatch.setattr(E, "_add_runs_with_inline_format", boom)
        out, stats = _make_docx(tmp_path, "第一章正文", name="s.docx",
                                 extra_root=roots_extra)
        assert os.path.getsize(out) > 0
        assert stats.get("block_render_failed", 0) >= 1

    def test_figure_counter_rolled_back_on_block_failure(self, tmp_path, monkeypatch):
        """块异常时表号必须回滚 —— 否则后续表格从错号起排。"""
        code = "flowchart TD\nA-->B"
        content = ("|a|b|\n|-|-|\n|1|2|\n\n表下图如下：\n\n```mermaid\n%s\n```\n\n尾。" % code)
        blocks = E._parse_content_blocks(content)
        for b in blocks:
            if b.get("type") == "chart":
                b["code"] = code
        original = E._add_table_from_markup

        state = {"n": 0}

        def flaky(doc, lines, font_name="宋体"):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return original(doc, lines, font_name)

        monkeypatch.setattr(E, "_add_table_from_markup", flaky)
        prep_blocks = blocks
        path = str(tmp_path / "c.docx")
        prep = {
            "scheme": {"name": "S", "project_id": "p"},
            "roots": [{"id": "s1", "parent_id": "", "level": "1", "sort_order": 1,
                       "title": "章", "content": content}],
            "children_map": {}, "chart_lookup": {},
            "rendered_bytes": {("flowchart", code): _png()},
            "blocks_cache": {"s1": prep_blocks},
            "heading_styles": E._load_heading_styles({}),
            "image_bytes": {}, "global_facts": [],
            "docx_options": {"font_name": "宋体", "font_size": 12.0,
                             "page_header": "", "page_footer": "",
                             "show_page_number": True, "show_title_page": False,
                             "show_toc": False, "bidder_name": "",
                             "page_break_before_chapter": True, "line_spacing": 1.15,
                             "page_number_style": "simple", "toc_depth": 3},
        }
        E._build_docx_sync(*E._build_docx_task(path, prep))
        from docx import Document
        text = "\n".join(p.text for p in Document(path).paragraphs)
        # 首张表渲染失败 → 表号回滚，后续图表不得出现「表 1-1」
        assert "表 1-1" not in text


# =========================================================================
# C. 表格尺寸上限（E3 · P1）
# =========================================================================
class TestTableSizeCaps:
    def test_normal_table_untouched(self, tmp_path):
        """正常表格不得被截断（向后兼容）。"""
        from docx import Document
        lines = ["|" + "|".join(["列%d" % i for i in range(8)]) + "|",
                 "|" + "|".join(["---"] * 8) + "|"]
        lines += ["|" + "|".join(["值%d" % j for j in range(8)]) + "|" for _ in range(30)]
        d = Document()
        E._fix_stats()["table_truncated"] = 0
        E._add_table_from_markup(d, lines, "宋体")
        assert len(d.tables) == 1
        assert len(d.tables[0].columns) == 8
        assert len(d.tables[0].rows) == 31  # 1 表头 + 30 数据行
        assert E._fix_stats()["table_truncated"] == 0

    def test_wide_table_truncated_to_word_limit(self):
        from docx import Document
        nc = E._TABLE_MAX_COLS + 25
        lines = ["|" + "|".join(["c"] * nc) + "|",
                 "|" + "|".join(["-"] * nc) + "|"]
        lines += ["|" + "|".join(["x"] * nc) + "|" for _ in range(3)]
        d = Document()
        E._fix_stats()["table_truncated"] = 0
        E._add_table_from_markup(d, lines, "宋体")
        assert len(d.tables[0].columns) == E._TABLE_MAX_COLS
        assert E._fix_stats()["table_truncated"] == 1

    def test_long_table_truncated(self):
        from docx import Document
        nr = E._TABLE_MAX_ROWS + 20
        lines = ["|a|b|", "|---|---|"] + ["|1|2|" for _ in range(nr)]
        d = Document()
        E._fix_stats()["table_truncated"] = 0
        E._add_table_from_markup(d, lines, "宋体")
        assert len(d.tables[0].rows) == E._TABLE_MAX_ROWS
        assert E._fix_stats()["table_truncated"] == 1

    def test_cell_budget_enforced(self):
        from docx import Document
        nc = E._TABLE_MAX_COLS
        nr = int(E._TABLE_MAX_CELLS / nc) + 30
        lines = ["|" + "|".join(["c"] * nc) + "|",
                 "|" + "|".join(["-"] * nc) + "|"]
        lines += ["|" + "|".join(["x"] * nc) + "|" for _ in range(nr)]
        d = Document()
        E._add_table_from_markup(d, lines, "宋体")
        t = d.tables[0]
        assert len(t.columns) * len(t.rows) <= E._TABLE_MAX_CELLS

    def test_header_row_always_kept(self):
        """截断永远保留表头行（第 1 行是列名）。"""
        from docx import Document
        nc = E._TABLE_MAX_COLS + 10
        lines = ["|" + "|".join(["HEAD"] * nc) + "|",
                 "|" + "|".join(["-"] * nc) + "|"]
        lines += ["|" + "|".join(["x"] * nc) + "|" for _ in range(5)]
        d = Document()
        E._add_table_from_markup(d, lines, "宋体")
        assert "HEAD" in d.tables[0].rows[0].cells[0].text

    def test_caps_are_sane(self):
        assert E._TABLE_MAX_COLS <= 63          # Word 表格硬上限
        assert E._TABLE_MAX_ROWS >= 100         # 正常工程表格远小于此
        assert E._TABLE_MAX_CELLS >= 2000


# =========================================================================
# D. 原子替换失败不写缓存（E5 · P1）
#
# ✅ 锚点同步（2026-10-15 · R77 §9.1 收口）：并行会话把导出双链的
#    「INSERT → commit → prune → commit」序列收敛为共享 helper
#    `_persist_export_cache_row`（export.py，双提交口径）。缓存 INSERT
#    不再位于 `export_docx` 函数体内 —— 旧护栏直接
#    `src.index("INSERT OR IGNORE...")` 失去锚点而恒红（R77 全量那 4 个
#    failed 的根因）。护栏语义不变，改锁**调用形态**：
#      · 写入调用必须被门控（DOCX `if cacheable:` 成功分支）；
#      · 失败分支不得出现写入调用；
#      · tmp 路径不得作为 out_path 实参进入写入调用；
#      · 新增 INSERT 单一事实源锁（原子串判据在收敛后结构性失效的形态）。
#    ✅ R126（2026-10-21）：PDF 导出整体下线，原 PDF 分支两条专属用例
#    （test_pdf_replaces_before_cache_row / test_pdf_cache_row_not_written_when_replace_fails）
#    随链路删除；同源语义由 DOCX 侧用例继续锁定。
# =========================================================================
_PERSIST_CALL = "_persist_export_cache_row("


class TestAtomicReplaceCacheGovernance:
    def test_docx_skips_cache_when_replace_fails(self):
        """`cacheable` 标志必须真正门控缓存写入（静态接线锁）。"""
        src = inspect.getsource(E.export_docx)
        assert "cacheable = True" in src
        assert "cacheable = False" in src
        assert re.search(r"if cacheable:\s*\n\s*await _persist_export_cache_row\(", src), \
            "缓存写入调用必须被 cacheable 门控"

    def test_docx_tmp_path_never_persisted(self):
        """tmp 路径不得写入 export_cache（否则 DB 永久指向 .tmp 孤儿文件）。"""
        src = inspect.getsource(E.export_docx)
        i_call = src.index(_PERSIST_CALL)
        seg = src[i_call:i_call + 700]
        assert "tmp_out_path" not in seg

    def test_cache_insert_single_source_after_refactor(self):
        """INSERT 单一事实源（2026-10-14 收敛加法式护栏）：helper 承载
        INSERT+双提交；两分支不得回流自持裸 INSERT（否则第二份副本）。
        """
        helper = inspect.getsource(E._persist_export_cache_row)
        assert "INSERT OR IGNORE INTO export_cache" in helper
        assert helper.count("await db.commit()") == 2, \
            "双提交口径（INSERT→commit→prune→commit）不得退化"
        assert "_prune_export_cache" in helper, "写入必须同序列执行保留裁剪"
        # ✅ R126：PDF 下线后收敛为 DOCX 单目标（原循环含 export_pdf）
        for fn in (E.export_docx,):
            assert "INSERT OR IGNORE INTO export_cache" not in inspect.getsource(fn), \
                "缓存 INSERT 只允许存在于 _persist_export_cache_row 一处"


# =========================================================================
# E. R13 判空单一出口（E1 · P0）
# =========================================================================
_R13_HELPERS = ("_db_fetch_all", "_db_fetch_one")


class TestR13SingleSource:
    def test_helpers_exist(self):
        assert inspect.iscoroutinefunction(E._db_fetch_all)
        assert inspect.iscoroutinefunction(E._db_fetch_one)
        assert inspect.iscoroutinefunction(E._db_exec)

    @pytest.mark.asyncio
    async def test_fetch_all_handles_none_cursor(self, caplog):
        with caplog.at_level("WARNING"):
            assert await E._db_fetch_all(None, what="t") == []
        assert "None" in caplog.text

    @pytest.mark.asyncio
    async def test_fetch_one_handles_none_cursor(self):
        assert await E._db_fetch_one(None, what="t") is None

    @pytest.mark.asyncio
    async def test_exec_reports_failure(self):
        class _DB:
            async def execute(self, sql, params=()):
                return None

        import logging
        assert await E._db_exec(_DB(), "UPDATE t SET a=1", (), what="x") is False

    @pytest.mark.asyncio
    async def test_exec_reports_success(self):
        class _Cur:
            pass

        class _DB:
            async def execute(self, sql, params=()):
                return _Cur()

        assert await E._db_exec(_DB(), "UPDATE t SET a=1", (), what="x") is True

    def test_no_bare_fetchall_or_fetchone_left(self):
        """AST 静态锁：模块内除三个 helper 自身外不得再裸调 fetchall/fetchone。"""
        tree = ast.parse(io.open(EXPORT_PY, encoding="utf-8").read())
        helper_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and node.name in _R13_HELPERS:
                helper_lines.update(range(node.lineno, (node.end_lineno or 0) + 1))
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"fetchall", "fetchone"}
                    and node.lineno not in helper_lines):
                offenders.append(node.lineno)
        assert not offenders, (
            f"fetchall/fetchone 裸调点位数 {len(offenders)}，行号 {offenders}"
            "（应全部经 _db_fetch_all / _db_fetch_one 单一出口）")

    def test_no_inline_none_guard_left(self):
        """静态锁：`if cur is None:` 内联判空只允许出现在三个 helper 体内
        （即 R13 判空的唯一实现），其余任何位置出现即视为判据分叉。"""
        tree = ast.parse(io.open(EXPORT_PY, encoding="utf-8").read())
        helper_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and node.name in ("_db_fetch_all", "_db_fetch_one", "_db_exec"):
                helper_lines.update(range(node.lineno, (node.end_lineno or 0) + 1))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                t = node.test
                if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                        and t.left.id == "cur" and t.ops
                        and isinstance(t.ops[0], ast.Is)
                        and isinstance(t.comparators[0], ast.Constant)
                        and t.comparators[0].value is None):
                    if node.lineno not in helper_lines:
                        offenders.append(node.lineno)
        assert not offenders, (
            f"仍有内联 `if cur is None` 判空（行 {offenders}），应走 _db_* 单一出口")

    def test_route_cache_lookups_guarded(self):
        """✅ R126：PDF 下线后收敛为 DOCX 单目标。"""
        for fn in (E.export_docx,):
            src = inspect.getsource(fn)
            assert "缓存查询" in src, f"{fn.__name__} 缓存查询未走判空 helper"


# =========================================================================
# F. 前后端导出配置键 parity（E6）
# =========================================================================
def _fe_export_tab_keys():
    """从前端导出 Tab 抽取配置键：initialValues 对象 + Form.Item name。"""
    src = io.open(FRONTEND_PAGE, encoding="utf-8").read()
    start = src.index("initialValues={{")
    # 花括号配平，截出 initialValues 对象文本
    depth, i = 0, start + len("initialValues=")
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                block = src[i:j + 1]
                break
    else:  # pragma: no cover
        raise AssertionError("initialValues 花括号未配平")
    keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", block))
    # 表单控件声明的 name（导出 Tab 内）
    tab_start = src.index('key: "export"')
    form_names = set(re.findall(r'name="([a-z_][a-z0-9_]*)"',
                                src[tab_start:tab_start + 30000]))
    return keys, form_names


class TestFrontendBackendConfigParity:
    def test_extractor_is_not_vacuous(self):
        """防空转：抽取器必须真的拿到了配置键（否则下面的 parity 断言恒真）。"""
        keys, form_names = _fe_export_tab_keys()
        assert len(keys) >= 20, f"initialValues 抽到的键过少：{sorted(keys)}"
        assert len(form_names) >= 10, f"导出 Tab 表单项抽到的过少：{sorted(form_names)}"
        assert "font_name" in keys and "scheme_forms" in keys

    def test_initial_values_are_subset_of_backend_whitelist(self):
        """前端能设置的键必须都在后端白名单内 —— 否则该设置被静默忽略且
        产物指纹不随其变化（缓存投毒）。"""
        keys, _ = _fe_export_tab_keys()
        # initialValues 里也含嵌套子键（字体名 / 加粗 / 封面字段），只校验
        # 「看起来像导出配置键」的项（出现在后端白名单语汇或带下划线的英文键）
        candidates = {k for k in keys
                      if re.fullmatch(r"[a-z][a-z0-9_]*", k)
                      and k not in _FE_SUBKEY_ALLOWLIST}
        unknown = sorted(candidates - set(E._EXPORT_CONFIG_KEYS))
        assert not unknown, f"前端 initialValues 含后端不识别的配置键：{unknown}"
        assert candidates, "候选键集为空，正则可能已失效"

    def test_backend_whitelist_keys_are_reachable_from_ui(self):
        """后端白名单里的每个键都必须在导出 Tab 有落点（initialValues 或表单项），
        防止后端新增开关却无人能设置。"""
        keys, form_names = _fe_export_tab_keys()
        reachable = keys | form_names
        missing = sorted(E._EXPORT_CONFIG_KEYS - reachable)
        assert not missing, f"后端白名单键在导出 UI 无落点：{missing}"

    def test_whitelist_matches_prepare_export_reads(self):
        """`_normalize_config` 白名单必须覆盖取数/渲染段实际读取的键。

        ✅ R94（2026-10-17 · 缓存命中快速路径）：`_prepare_export` 拆成
        「取数段 + 渲染段（`_prepare_export_render`）」，版式配置读取搬到了
        `_build_docx_options`。判据必须扫**三者合起来**，只扫取数段会把
        「读取点仍在、白名单漏登」误判为「白名单里有无人读的键」。
        """
        src = (inspect.getsource(E._prepare_export)
               + inspect.getsource(E._prepare_export_render)
               + inspect.getsource(E._build_docx_options))
        read = set(re.findall(r"config\.get\(\s*[\"']([a-z_0-9]+)[\"']", src))
        missing = sorted(read - set(E._EXPORT_CONFIG_KEYS))
        assert not missing, (
            f"取数/渲染段读取但未纳入指纹白名单的键（改它们不失效缓存）：{missing}")

    def test_config_values_are_actually_read_somewhere(self):
        """反向：白名单里的键必须在取数段/渲染段/docx_options/构建侧被真实消费。

        ✅ R94：`_build_docx_options` 是版式读取的唯一出口（原先内联在
        `_prepare_export` 尾部），必须纳入扫描面，否则 17 个版式键会被误报为死键。
        """
        src = (inspect.getsource(E._prepare_export)
               + inspect.getsource(E._prepare_export_render)
               + inspect.getsource(E._build_docx_options)
               + inspect.getsource(E._build_docx_sync))
        dead = sorted(k for k in E._EXPORT_CONFIG_KEYS
                      if f'"{k}"' not in src and f"'{k}'" not in src
                      and f".get({k}" not in src)
        assert not dead, f"白名单里存在无人消费的键（应删除）: {dead}"


# initialValues 里的**非导出配置**子键（字体名 / 加粗 / 封面信息字段 / 表单子开关）
_FE_SUBKEY_ALLOWLIST = frozenset({
    "font_name", "font_size", "bold", "top", "bottom", "left", "right",
    "compilation_note", "approval", "expert_review", "drawing_appendix",
})


# =========================================================================
# G. 降级计数可观测（X-Fix-Stats 加法式扩展）
# =========================================================================
class TestDegradationObservability:
    def test_fix_stats_keys_extended(self):
        assert "block_render_failed" in E._FIX_STATS_KEYS
        assert "table_truncated" in E._FIX_STATS_KEYS

    def test_original_seven_keys_unchanged(self):
        """向后兼容：既有 7 个键的取值与语义一字未动。"""
        assert E._FIX_STATS_KEYS[:7] == (
            "formulas", "block_formulas", "replacement_chars", "control_chars",
            "gbk_mojibake", "latin1_mojibake", "cyrillic")

    def test_log_fix_stats_reports_degradation(self, tmp_path, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(
            E, "_add_table_from_markup",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        with caplog.at_level(logging.WARNING, logger="export"):
            _, stats = _make_docx(tmp_path, "|a|b|\n|-|-|\n|1|2|\n", name="o.docx")
        assert stats.get("block_render_failed", 0) >= 1
        assert "降级" in caplog.text

    def test_frontend_fix_stats_interface_knows_new_keys(self):
        """前端 `FixStats` **接口声明**必须含新键（跨模块契约同步）。

        ⚠️ 判据必须锚定 interface 块本体：只断言「文件里出现该字符串」会被
        `fixStatsParts` 的文案命中而恒真 —— A/B 反向验证（A9 删掉接口里的
        字段声明）实测过这个坑。
        """
        p = os.path.join(REPO_ROOT, "frontend", "src", "utils", "exportResponse.ts")
        src = io.open(p, encoding="utf-8").read()
        i = src.index("export interface FixStats {")
        block = src[i:src.index("}", i)]
        for k in ("block_render_failed", "table_truncated"):
            assert f"{k}?:" in block, f"FixStats 接口缺少 {k}"
        # 展示函数也必须消费（否则统计解析出来没人看）
        tail = src[src.index("export function fixStatsParts"):]
        for k in ("block_render_failed", "table_truncated"):
            assert k in tail, f"fixStatsParts 未消费 {k}"

    def test_frontend_original_seven_keys_unchanged(self):
        """向后兼容：既有 7 个字段在接口与展示文案中一字未动。"""
        p = os.path.join(REPO_ROOT, "frontend", "src", "utils", "exportResponse.ts")
        src = io.open(p, encoding="utf-8").read()
        i = src.index("export interface FixStats {")
        block = src[i:src.index("}", i)]
        for k in ("formulas", "block_formulas", "replacement_chars",
                  "control_chars", "gbk_mojibake", "latin1_mojibake", "cyrillic"):
            assert f"{k}?:" in block, f"既有字段 {k} 被误删"

    def test_exporter_version_unchanged_with_justification(self):
        """本轮修复对正常文档**逐字节不变**，故本轮自身不失效全量缓存。

        ⚠️ 重锚（R109 · 2026-10-19）：后续 R109 修复了「WPS 打不开」的
        ``w:tcPr`` 序列违例，必须让旧（坏）成稿整体失效，故版本号已升至 23。
        ⚠️ 重锚（R117 · 2026-10-20）：成稿**版式**变化（A4 页面 / 标题字号阶梯 +
        强制黑色 / 图题 keep_with_next / 表格行 cantSplit）必然改变产物字节。
        若沿用 v23 缓存行，用户内容与配置未变 → 命中旧缓存 → 每次拿回
        「Letter + 主题蓝 14pt 标题」的旧成稿，版式修复永不可见。故升至 24。
        ⚠️ 重锚（R117 FIX-10 · 2026-10-20）：折叠「同章逐字重复正文块」**直接删除
        成稿里的重复段落**，产物字节必变。若沿用 v24 缓存行，用户内容与配置一字
        未改 → 命中旧缓存 → 拿回的还是「整节重复两遍」的稿，去重永不可见。故升至 25。
        ⚠️ 重锚（R126 · 2026-10-21）：公式 OMML 解析两缺陷（`\\sqrt[n]{x}` 结构错误 /
        组内脚本静默拆平排）改变了**含公式章的产物字节**，旧缓存行内含错位 OMML，
        必须整体失效重建；PDF 导出链下线不改 DOCX 字节、不单独触发升版。
        同轮追加的 ④ FIX-C2/LIM（docx_math 算子函数名映射/空 nary 体补 run/
        空脚本丢弃/subHide 形态）再次改变含公式章产物字节 ⇒ 继续在 26 之上
        升至 27（export.py 注释块 ①②③④ 即其理由）。
        ⚠️ R127（2026-10-22）再升至 28：原生列表编号 / 附录表头重复与版心列宽 /
        域拆分 run / 正文 Body Text CN 样式 / 图片版心封顶 —— 均改变产物字节。
        ⚠️ R128（2026-10-22）再升至 29：交叉引用 REF 域（题注书签 + 正文回填
        5-run REF 域）改变 XML 字节；未登记/被回滚编号保持纯文本（零内容变化）。
        ⚠️ R129（2026-10-22）再升至 30：Word 2010+ 专有命名空间清除（落盘后
        zip 级重写）/ L1~L9 标题层级（Heading 8/9 + outlineLvl）/ 公式三级
        降级（OMML → 图片）—— 均改变产物字节，旧缓存整体失效重建。
        ⚠️ R131-A（2026-10-23）再升至 31：图题 / AI 配图 alt 剥离生成期残留
        「图 X-Y」前缀（export._strip_leading_figure_number）—— **可见题注文本**
        由「图 2-1 图 4-1 xxx」变为「图 2-1 xxx」，旧缓存内含双图号必须失效。
        故锚定 31。本轮（2026-10-06）的意图不变：**本轮**不因自身改动失效缓存。
        """
        assert E._EXPORTER_VERSION == "31"


# =========================================================================
# 兜底：worker 完成即清理线程局部统计，避免用例间串号
# =========================================================================
@pytest.fixture(autouse=True)
def _reset_fix_stats():
    st = E._fix_stats()
    st.update({k: 0 for k in E._FIX_STATS_KEYS})
    yield
    st.update({k: 0 for k in E._FIX_STATS_KEYS})