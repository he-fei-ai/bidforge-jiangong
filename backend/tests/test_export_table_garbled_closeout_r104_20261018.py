# -*- coding: utf-8 -*-
"""导出乱码收口护栏（R104 · 2026-10-18）

用户报障：导出的专项方案文档出现「乱码」。取证（用户桌面《导出文档内容乱码.txt》
→ 生产库 scheme_assistant.db sections 逐行比对）确认两类根因，本轮修复并锁死：

  FIX-1（P0 · 表格管道语法泄漏进交付文档）
    AI 生成的 GFM 表格有两种实测坏形态，而 ``content_blocks`` 的表格判据要求
    分隔行必须落在 ``tbl_lines[1]``，对它们**整块失效** → 整表降级为裸文本
    段落，``| --- | --- |`` 管道语法原样印进 DOCX/PDF（即用户看到的乱码）：
      A. **分隔行前置**（rowid 227）：``| --- |`` 写在表头行之前；
      B. **多表粘连**（rowid 140）：两张表之间无空行，所有 ``|`` 行并为一个块。
    修法：``content_blocks._split_table_block`` 按分隔行切分重排为规范序
    ``[表头, 分隔行, *数据]``（GFM 语义：分隔行永远紧跟表头）；规范序块与旧
    行为逐字节一致；切分失败（无分隔行/表头不可信/有未覆盖行）才走旧降级
    路径，绝不 Fabrication。

  FIX-2（P1 · 西里尔单字母形近字误植）
    同一批章节里 ``ф48.3mm×3.6mm`` / ``ф14`` 用的是西里尔 ф(U+0444) 而非
    GB 文档的直径符号 φ（希腊 Phi, U+03C6）—— preflight「钢管壁厚」判据只认
    ``[φΦ]``，形近字还会让事实抽取对不上号。既有西里尔修复只覆盖 ≥2 连续
    字母的词段，单字母符号误植结构性漏修（全库 6 处全部漏进交付文档）。
    修法：封闭映射 ``ф/Ф → φ/Φ``，仅「后随数字 + 前非西里尔字母」的高置信
    形态命中（排除俄语词内字母），计数并入既有 ``cyrillic`` 统计键。

分组：
  A 组 · FIX-1 表格切分功能实证（规范序零回归 / 分隔行前置 / 多表粘连 /
       旧降级路径保持 / 病态形态不 Fabrication）
  B 组 · FIX-2 西里尔形近字功能实证（真实单元格文本 / 俄语词不误伤 /
       统计口径 / 宁可不改）
  C 组 · 渲染层端到端（_add_table_from_markup → 真 Word 表格 + φ 修复）
  D 组 · export_docx 全链路端到端（生产真实正文 → 成稿无管道语法残留）
  E 组 · 静态锁 + 字节体检（单一实现 AST 锁 / CRLF·BOM·FFFD 卫生）

判据纪律（AGENTS §5.14/§5.15）：静态锁锚 AST 节点；每个修复点均配「触发行为」
的功能实证，防静态锁的结构性盲区（R98 M2 教训）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from app.routers.export import _add_table_from_markup
from app.services.content_blocks import _parse_content_blocks, _split_table_block
from app.services.docx_math import clean_text, scan_issues

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# ---------------------------------------------------------------------------
# 生产取证素材（scheme_assistant.db sections rowid 227 / 140，逐字摘录）
# ---------------------------------------------------------------------------
#: 分隔行前置 + 表题行（rowid 227）
SEP_FIRST_CONTENT = (
    "专项演练主要包括高处坠落、物体打击、坍塌及触电事故应急处置演练，频次见表 8-3。\n"
    "\n"
    "表 8-3 模板工程应急演练频次安排表\n"
    "| --- | --- | --- | --- |\n"
    "| 演练类型 | 频次要求 | 组织单位 | 参与人员范围 |\n"
    "| 综合应急演练 | 每年不少于 1 次 | 项目部 | 全体管理人员及作业班组 |\n"
    "| 高处坠落救援演练 | 每季度不少于 1 次 | 安全部牵头 | 安全员、急救员、相关作业人员 |\n"
    "| 坍塌事故应急处置演练 | 每季度不少于 1 次 | 技术部牵头 | 技术员、安全员、架子工班组 |\n"
    "| 触电事故应急救援演练 | 每半年不少于 1 次 | 机电组牵头 | 电工、安全员、相关作业人员 |\n"
    "| 消防应急演练 | 每半年不少于 1 次 | 安全部牵头 | 全体在场人员 |\n"
)
#: 多表粘连：表 1 分隔行前置，表 2 规范序，两张表之间**无空行**（rowid 140）
GLUED_CONTENT = (
    "## 2.2 主体结构模板概况\n"
    "| --- | --- |\n"
    "| 构件类型 | 主要选型配置 |\n"
    "| 框架柱 | 15mm厚覆膜防水胶合板，竖向50mm×100mm木龙骨，双ф48.3mm×3.6mm钢管围檩，ф14对拉螺栓 |\n"
    "| 框架梁（截面高度≤700mm） | 15mm厚覆膜防水胶合板，横向50mm×100mm木龙骨@250mm，底模下设ф48.3mm×3.6mm钢管横楞@500mm |\n"
    "| 楼板（板厚≤150mm） | 15mm厚覆膜防水胶合板，主龙骨ф48.3mm×3.6mm钢管@1000mm |\n"
    "| 电梯井道内衬模板（若需新浇筑部位） | 18mm厚多层板，定型钢背楞或钢管加强，满足垂直度控制要求 |\n"
    "| 序号 | 控制项目 | 允许偏差（mm） | 检验方法 |\n"
    "| --- | --- | --- | --- |\n"
    "| 1 | 轴线位置 | 5 | 钢尺检查 |\n"
    "| 2 | 底模上表面标高 | ±5 | 水准仪或拉线、钢尺检查 |\n"
    "| 3 | 截面内部尺寸：柱、墙、梁 | +4，-5 | 钢尺检查 |\n"
    "| 4 | 层高垂直度：不大于5m | 6 | 经纬仪或吊线、钢尺检查 |\n"
)


def _tables(content: str) -> list[dict]:
    return [b for b in _parse_content_blocks(content) if b.get("type") == "table"]


def _paragraphs(content: str) -> list[dict]:
    return [b for b in _parse_content_blocks(content) if b.get("type") == "paragraph"]


# ---------------------------------------------------------------------------
# A 组 · FIX-1 表格切分功能实证
# ---------------------------------------------------------------------------

class TestTableSplitFunctionalA:
    """触发行为即红：摘除 _split_table_block 接线或改坏判据，本组全部翻红。"""

    def test_normal_table_byte_identical(self):
        """规范序表格（分隔行在 index 1）→ 单 table 块，lines 与输入逐字节一致。

        这是零回归锁：修复前该路径产出的块必须一字不改。
        """
        lines = [
            "| 项目 | 允许偏差（mm） | 检验方法 |",
            "| --- | --- | --- |",
            "| 轴线位置 | 5 | 钢尺检查 |",
            "| 底模上表面标高 | ±5 | 水准仪检查 |",
        ]
        blocks = _tables("\n".join(lines))
        assert len(blocks) == 1
        assert blocks[0]["lines"] == lines  # 逐字节一致
        assert "caption" not in blocks[0]

    def test_separator_first_reordered_with_caption(self):
        """分隔行前置（rowid 227 真实形态）→ 重排为规范序 + 表题行被吸收。"""
        tables = _tables(SEP_FIRST_CONTENT)
        assert len(tables) == 1
        lines = tables[0]["lines"]
        # 表头必须回到 index 0，分隔行落到 index 1（export._add_table_from_markup
        # 的契约：跳过 index 1；write_section 的自动表题取 lines[0]）
        assert lines[0] == "| 演练类型 | 频次要求 | 组织单位 | 参与人员范围 |"
        assert lines[1] == "| --- | --- | --- | --- |"
        assert len(lines) == 7  # 表头 + 分隔行 + 5 数据行
        # 表题行升格为 caption，不再作为正文段落输出
        assert tables[0].get("caption") == "模板工程应急演练频次安排表"
        texts = [p.get("text") or "" for p in _paragraphs(SEP_FIRST_CONTENT)]
        assert not any("模板工程应急演练频次安排表" in t for t in texts)
        assert not any("|" in t for t in texts), "管道语法不得残留为正文段落"

    def test_glued_tables_split_into_two(self):
        """多表粘连（rowid 140 真实形态）→ 按分隔行切成两张规范序表。"""
        tables = _tables(GLUED_CONTENT)
        assert len(tables) == 2, "两张表之间无空行，必须切成两个 table 块"
        t1, t2 = tables
        assert t1["lines"][0] == "| 构件类型 | 主要选型配置 |"
        assert t1["lines"][1] == "| --- | --- |"
        assert len(t1["lines"]) == 6  # 表头 + 分隔行 + 4 数据行
        assert t2["lines"][0] == "| 序号 | 控制项目 | 允许偏差（mm） | 检验方法 |"
        assert t2["lines"][1] == "| --- | --- | --- | --- |"
        assert len(t2["lines"]) == 6  # 表头 + 分隔行 + 4 数据行
        # 不得有任何管道语法残留为段落
        assert all("|" not in (p.get("text") or "")
                   for p in _paragraphs(GLUED_CONTENT))

    def test_no_separator_falls_back_to_paragraphs(self):
        """无分隔行 → 旧降级路径保持（整块按段落输出，内容不丢）。"""
        content = "| 这不是表 | 只是文本 |\n| 第二行 | 同样 |\n"
        assert _tables(content) == []
        paras = [p.get("text") or "" for p in _paragraphs(content)]
        assert paras == ["| 这不是表 | 只是文本 |", "| 第二行 | 同样 |"]

    def test_single_pipe_line_stays_paragraph(self):
        """单行 `| 文字 |`（长度 < 2）→ 段落，不 Fabrication。"""
        content = "| 孤立一行 |\n"
        assert _tables(content) == []
        assert [p.get("text") for p in _paragraphs(content)] == ["| 孤立一行 |"]

    def test_leading_orphan_rows_not_fabricated(self):
        """前导孤儿数据行（首个分隔行前有未覆盖行）→ 整体降级为段落。

        半识别会把孤儿行静默丢掉，违背「宁可不改，不丢内容」。
        """
        content = "| 孤儿行 | 甲 |\n| 表头 | 乙 |\n| --- | --- |\n| 1 | 2 |\n"
        assert _tables(content) == []
        paras = [p.get("text") or "" for p in _paragraphs(content)]
        assert paras == ["| 孤儿行 | 甲 |", "| 表头 | 乙 |",
                         "| --- | --- |", "| 1 | 2 |"]

    def test_consecutive_separators_not_fabricated(self):
        """连续两个分隔行（表头不可信）→ 整体降级为段落，不 Fabrication。"""
        content = "| 表头 | 甲 |\n| --- | --- |\n| --- | --- |\n| 1 | 2 |\n"
        assert _tables(content) == []
        assert len(_paragraphs(content)) == 4

    def test_split_table_block_direct_contract(self):
        """_split_table_block 直调契约：全覆盖才返回段；空输入/无分隔行 → []。"""
        assert _split_table_block([]) == []
        assert _split_table_block(["| a | b |"]) == []
        # 分隔行前置 → 规范序单段
        seg = _split_table_block(["| --- |", "| h |", "| 1 |"])
        assert seg == [["| h |", "| --- |", "| 1 |"]]
        # 粘连 → 两段
        seg = _split_table_block(["| --- |", "| h1 |", "| d |", "| h2 |", "| --- |", "| d2 |"])
        assert seg == [["| h1 |", "| --- |", "| d |"],
                       ["| h2 |", "| --- |", "| d2 |"]]


# ---------------------------------------------------------------------------
# B 组 · FIX-2 西里尔形近字功能实证
# ---------------------------------------------------------------------------

class TestCyrillicGlyphFunctionalB:
    """触发行为即红：摘除 _CYRILLIC_GLYPH_NUM_RE 接线，本组翻红。"""

    @pytest.mark.parametrize("raw,want", [
        # 生产真实单元格文本（rowid 140）
        ("双ф48.3mm×3.6mm钢管围檩，ф14对拉螺栓",
         "双φ48.3mm×3.6mm钢管围檩，φ14对拉螺栓"),
        ("底模下设ф48.3mm×3.6mm钢管横楞@500mm",
         "底模下设φ48.3mm×3.6mm钢管横楞@500mm"),
        ("Ф100mm 导管", "Φ100mm 导管"),
        ("ф 48 钢管", "φ 48 钢管"),          # 允许符号与数字间有空白
        ("ф8@200 箍筋", "φ8@200 箍筋"),
    ])
    def test_misplanted_cyrillic_glyph_restored(self, raw, want):
        assert clean_text(raw) == want

    def test_glyph_without_digit_untouched(self):
        """后无数字 → 不命中（宁可不改，不丢内容）。"""
        assert clean_text("采用ф钢丝网") == "采用ф钢丝网"
        assert clean_text("ф") == "ф"

    def test_cyrillic_inside_word_untouched(self):
        """俄语词内字母不误伤（前一个字符是西里尔字母 → 排除）。

        第二个用例锁死「前非西里尔字母」守护本身：``проф5`` 这类以 ф 结尾的
        俄语词直接后随数字，若去掉负向回顾就会被改写成 ``проφ5``（静默篡改
        引文内容，违反「宁可不改，不丢内容」）。
        """
        assert clean_text("俄语版本5 протокол") == "俄语版本5 протокол"
        assert clean_text("ф48.3 与 версия5 混排") == "φ48.3 与 версия5 混排"
        assert clean_text("проф5 报告") == "проф5 报告"
        assert clean_text("граф5") == "граф5"

    def test_scan_issues_counts_single_glyph_form(self):
        """统计口径：单字母形近字必须计入 cyrillic（旧口径恒 0 = 修复无观测）。"""
        s = scan_issues("ф48.3mm×3.6mm，ф14")
        assert s["cyrillic"] == 2
        assert scan_issues("普通中文文本，无西里尔")["cyrillic"] == 0

    def test_mixed_with_word_form_counts_both(self):
        """词段 + 单字母两种形态同时存在时计数相加、各自修复。"""
        # "кампания"（词段，未命中词表保留原样）+ "ф10"（单字母形近字）
        s = scan_issues("кампания ф10")
        assert s["cyrillic"] == 2
        assert clean_text("кампания ф10") == "кампания φ10"


# ---------------------------------------------------------------------------
# C 组 · 渲染层端到端（_add_table_from_markup）
# ---------------------------------------------------------------------------

class TestTableRenderEndToEndC:
    """粘连坏形态正文经渲染层产出真 Word 表格，且单元格内 ф 已修复为 φ。"""

    def test_glued_content_renders_real_tables(self):
        from docx import Document

        tables = _tables(GLUED_CONTENT)
        assert len(tables) == 2
        doc = Document()
        for t in tables:
            _add_table_from_markup(doc, t["lines"], "宋体")
        assert len(doc.tables) == 2
        # 表 1：2 列 × 5 行（表头 + 4 数据行，分隔行不渲染）
        assert len(doc.tables[0].columns) == 2
        assert len(doc.tables[0].rows) == 5
        # 表 2：4 列 × 5 行
        assert len(doc.tables[1].columns) == 4
        assert len(doc.tables[1].rows) == 5
        # 单元格文本不得含管道语法；ф 必须已修复为 φ
        cell_texts = [c.text for tb in doc.tables for r in tb.rows for c in r.cells]
        assert not any("|" in t for t in cell_texts)
        assert any("φ48.3mm×3.6mm" in t for t in cell_texts)
        assert not any("ф" in t for t in cell_texts)
        # 表头行加粗（render 层既有契约：ri == 0 加粗）
        assert doc.tables[0].rows[0].cells[0].paragraphs[0].runs[0].bold

    def test_auto_caption_uses_reordered_header(self):
        """自动表题路径（无表题行）：lines[0] 必须是真表头（构件类型…）。"""
        tables = _tables(GLUED_CONTENT)
        first = tables[0]["lines"][0]
        cells = [c.strip() for c in first.split("|") if c.strip()]
        assert cells == ["构件类型", "主要选型配置"]


# ---------------------------------------------------------------------------
# D 组 · export_docx 全链路端到端（生产真实正文）
# ---------------------------------------------------------------------------

async def _seed_scheme(db_conn, content: str):
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s104", "p104", "模板工程专项施工方案"),
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("c104", "s104", "", 0, 1, "第8章 模板工程", "generated", content),
    )
    await db_conn.commit()


@pytest.mark.asyncio
async def test_export_docx_no_pipe_garbling(db_conn):
    """端到端：分隔行前置 + 多表粘连 + 西里尔误植的真实正文导出后，
    成稿必须是真表格、无管道语法残留、ф 已修复、X-Fix-Stats 有计数。"""
    from app.routers.export import export_docx
    from fastapi.responses import FileResponse

    await _seed_scheme(db_conn, GLUED_CONTENT)
    resp = await export_docx("s104", {
        "project_name": "示例项目",
        "bid_section": "第一标段",
        "config": {"allow_pil_fallback": True},
        "chart_images": [],
        "fe_codes": [],
    }, db=db_conn)
    assert isinstance(resp, FileResponse)
    path = resp.path
    assert path

    from docx import Document
    doc = Document(path)

    # 两张真 Word 表格（粘连块被正确切分）
    assert len(doc.tables) == 2, "多表粘连正文必须渲染为两张真表格"
    assert len(doc.tables[0].columns) == 2
    assert len(doc.tables[1].columns) == 4

    # 全文不得残留管道表格语法（乱码本体）
    all_text = "\n".join(p.text for p in doc.paragraphs)
    cell_texts = [c.text for tb in doc.tables for r in tb.rows for c in r.cells]
    joined = all_text + "\n" + "\n".join(cell_texts)
    assert "| --- |" not in joined, "成稿仍残留 GFM 分隔行语法（乱码未修复）"
    assert "| 构件类型 |" not in joined, "成稿仍残留管道表格行（乱码未修复）"

    # 西里尔误植修复进入交付单元格
    assert "ф" not in "\n".join(cell_texts), "成稿仍含西里尔 ф"
    assert any("φ48.3mm×3.6mm" in t for t in cell_texts)

    # 表格题注存在（GB/T：表题在表格上方；无表题行时由表头自动生成）
    assert "构件类型、主要选型配置" in all_text, "粘连表 1 应得自动表题"
    assert "序号、控制项目" in all_text, "粘连表 2 应得自动表题"

    # X-Fix-Stats：西里尔修复计数可观测
    stats = json.loads(resp.headers.get("X-Fix-Stats", "{}"))
    assert stats.get("cyrillic", 0) >= 4, f"西里尔修复未计数: {stats}"


@pytest.mark.asyncio
async def test_export_docx_separator_first_with_caption(db_conn):
    """端到端：分隔行前置 + 源文表题行 → 真表格 + 规范表题（不叠写编号）。"""
    from app.routers.export import export_docx
    from docx import Document
    from fastapi.responses import FileResponse

    await _seed_scheme(db_conn, SEP_FIRST_CONTENT)
    resp = await export_docx("s104", {
        "project_name": "示例项目",
        "bid_section": "第一标段",
        "config": {"allow_pil_fallback": True},
        "chart_images": [],
        "fe_codes": [],
    }, db=db_conn)
    doc = Document(resp.path)
    assert len(doc.tables) == 1
    assert len(doc.tables[0].columns) == 4
    assert len(doc.tables[0].rows) == 6  # 表头 + 5 数据行（分隔行不渲染）
    joined = "\n".join(p.text for p in doc.paragraphs)
    assert "| --- |" not in joined
    # 源文表题被吸收为导出表题（统一编号 + 剥离源文自写的「表 8-3」前缀，
    # 不叠写；正文引导句里的「见表 8-3」是合法引用，不受影响）
    assert "模板工程应急演练频次安排表" in joined
    assert "表 8-3 模板工程" not in joined, "源文自带编号不得叠写进表题"


# ---------------------------------------------------------------------------
# E 组 · 静态锁 + 字节体检
# ---------------------------------------------------------------------------

class TestStaticLocksAndByteHealthE:

    def test_split_table_block_single_definition(self):
        """_split_table_block 在全仓 AST 中必须只有唯一定义（防副本漂移）。"""
        tree = ast.parse((APP_DIR / "services" / "content_blocks.py")
                         .read_bytes().decode("utf-8"))
        defs = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_split_table_block"]
        assert len(defs) == 1

    def test_export_table_markup_skips_index_one(self):
        """export._add_table_from_markup 的契约未变：仍以 index 1 为分隔行跳过。

        这锁死「解析层必须产出规范序」的跨模块契约——若有人把渲染层也改成
        宽容形态，两处判据就会分叉（登记/渲染两侧再次各写一份）。
        """
        # 拆分兼容：聚合读取 export.py 与其子模块（_add_table_from_markup 已拆至 _export_docx_parts.py）
        import glob as _glob
        segs = [(APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8")]
        for p in sorted(_glob.glob(str(APP_DIR / "routers" / "_export_*.py"))):
            segs.append(open(p, "rb").read().decode("utf-8"))
        src = "\n".join(segs)
        assert "if j != 1" in src, "渲染层跳过 index 1 的契约被改写"

    def test_cyrillic_glyph_regex_single_definition(self):
        """_CYRILLIC_GLYPH_NUM_RE 唯一定义 + 封闭映射表非空。"""
        src = (APP_DIR / "services" / "docx_math.py").read_bytes().decode("utf-8")
        assert src.count("_CYRILLIC_GLYPH_NUM_RE = re.compile") == 1
        assert '_CYRILLIC_GLYPH_MAP = {"ф": "φ", "Ф": "Φ"}' in src

    @pytest.mark.parametrize("rel", [
        "services/content_blocks.py",
        "services/docx_math.py",
    ])
    def test_byte_health(self, rel):
        """字节体检：CRLF 工作树约定、无 BOM、无 U+FFFD、无孤立 CR。"""
        raw = (APP_DIR / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "不得带 UTF-8 BOM"
        assert raw.count(b"\r") == raw.count(b"\r\n"), "不得有孤立 CR"
        assert raw.decode("utf-8").count("\ufffd") == 0, "不得含 U+FFFD"
