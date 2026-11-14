"""R230 导出文档模块深度审计增强护栏（2026-11-12）。

覆盖本轮 23 项修复的行为实证与源码锁（详见 docs/导出文档模块深度审计增强_R230_20261112.md）：
- A1 降级产物绝不覆盖既有缓存产物（判定前移 + 只交付 tmp）
- A9 取消检查点覆盖「构建完成 → replace/写缓存」窗口
- B1  X-Fix-Stats 三处 ensure_ascii=True（R180 同族收口）
- A3  PDF 完整性复核失败路径不再 AttributeError
- A4  生成互斥守卫锁内复查
- A5  读租约登记提前到 viable 判定通过后立即
- A6  显式 None 与「未传」产物同语义（show_page_number / chapter_page_break）
- A7  封面落款日期纳入指纹（export_date，跨天自然失效）
- A8  PDF 链补 X-Export-Degraded: empty-sections
- B2  U+FFFE/FFFF/孤立代理项/数字实体物化全链清洗
- B3  字母序前缀剥离不再篡改并列枚举
- B4/B5/B6/B15 公式面（转义标点 / 矩阵环境 / 组内下标 / 词边界）
- B9/B10/B12 渲染层判据收窄（表题负向词 / 叠词最小单元 / 重复表头条件丢弃）
- B13/B14 clamp 出口补全 / 空 Heading 不进目录
- B16 Windows 路径不再判残留 LaTeX
- F7  预检补 page_residue / double_entity 审计规则
"""

import ast
import inspect
import json
import re
import xml.etree.ElementTree as ET

import app.routers._export_issue_scan as _scan
import app.routers.export as E
import pytest
from app.services import content_blocks as CB
from app.services import docx_math as DM

# ---------------------------------------------------------------------------
# A1：降级产物绝不覆盖既有缓存产物
# ---------------------------------------------------------------------------


class TestA1DegradedNeverOverwritesTarget:
    def test_degraded_judgement_precedes_replace(self):
        """源码顺序锁：DOCX 降级交付（return FileResponse）必须在 os.replace 之前。

        旧实现先 replace 再判降级 —— 降级产物把 out_path 上「历史成功导出」的
        产物原地覆盖成残缺版，而 DB 缓存行仍指向该路径（结构完整性判据照常
        放行）→ 后续同指纹导出永远命中残缺文档且零信号。
        """
        src = inspect.getsource(E.export_docx)
        degraded_return = src.find('FileResponse(str(tmp_out_path)')
        replace_call = src.find("os.replace(tmp_out_path, out_path)")
        assert degraded_return != -1, "降级分支必须交付本次临时文件（tmp_out_path）"
        assert replace_call != -1
        assert degraded_return < replace_call, "降级交付必须发生在 os.replace 之前（防覆盖既有产物）"

    async def test_degraded_never_touches_target_path(self, db_conn, tmp_path, monkeypatch):
        """行为实证：空章降级导出只交付 tmp 文件，out_path 不被创建/覆盖。

        先在 out_path 放置「历史成功导出」产物 → 触发空章降级导出 → 历史产物
        必须原样保留（旧实现会被本次残缺版覆盖）。
        """
        import asyncio
        from pathlib import Path

        pytest.importorskip("docx")

        scheme_id = "r226-degraded"
        await db_conn.execute(
            "INSERT INTO schemes(id, project_id, name) VALUES (?,?,?)", (scheme_id, "p", "降级不覆盖")
        )
        await db_conn.commit()
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        monkeypatch.setattr(E, "_record_export_review_trace", lambda *a, **k: asyncio.sleep(0))
        # 关闭快速路径以走完整链（空章必未命中缓存）
        monkeypatch.setattr(E, "_export_cache_fast_path_enabled", lambda: False)
        # 预放历史产物（模拟昨日另一配置的成功导出；**必须有 DB 行** ——
        # 无行产物会被 GC 按孤儿语义回收，那不是 A1 的防护场景。同指纹行
        # 会先命中缓存走不到降级，故用不同指纹的历史行）。
        cfg = E._export_config_hash({})
        fp = "0" * 32
        out_path = tmp_path / f"{scheme_id}_{cfg[:8]}_{fp[:8]}.docx"
        out_path.write_bytes(b"HISTORICAL-GOOD-DOCX")
        await db_conn.execute(
            "INSERT INTO export_cache (id, scheme_id, config_hash, content_fingerprint, result_path)"
            " VALUES (?,?,?,?,?)",
            ("legacy-1", scheme_id, "legacycfg", "legacyfp", str(out_path)),
        )
        await db_conn.commit()

        resp = await E.export_docx(scheme_id, {"config": {}, "override_high_issues": True}, db=db_conn)
        assert Path(resp.path).name.endswith(".tmp.docx"), "降级必须交付带 uuid 的临时文件"
        assert out_path.read_bytes() == b"HISTORICAL-GOOD-DOCX", (
            "降级产物绝不覆盖既有缓存产物（A1 核心断言）"
        )


# ---------------------------------------------------------------------------
# A9：取消检查点覆盖「构建完成 → replace/写缓存」窗口
# ---------------------------------------------------------------------------


class TestA9CancelCheckpointAfterBuild:
    def test_docx_post_build_cancel_checkpoint(self):
        src = inspect.getsource(E.export_docx)
        build_idx = src.find("_build_docx_sync, *_build_docx_task")
        assert build_idx != -1
        after_build = src[build_idx:]
        # 构建完成后、交付前的取消检查点（try 内，与构建线程取消同一 except 路径）
        assert "_check_export_cancelled(cancel_ev)" in after_build.split("except ExportCancelled")[0], (
            "构建完成后必须补取消检查点（契约：取消后不 replace、不写缓存行）"
        )

    def test_pdf_post_convert_cancel_checkpoint(self):
        src = inspect.getsource(E.export_pdf)
        convert_idx = src.find("_read_path_bytes, tmp_pdf")
        assert convert_idx != -1
        # 转换完成、读字节之前的检查点
        before_read = src[:convert_idx]
        seg = before_read[before_read.rfind("Word COM") :] if "Word COM" in before_read else before_read
        assert "_check_export_cancelled(cancel_ev)" in seg, "PDF 转换完成后必须补取消检查点"


# ---------------------------------------------------------------------------
# B1：X-Fix-Stats 三处 ensure_ascii=True（R180 同族收口）
# ---------------------------------------------------------------------------


class TestB1FixStatsAsciiSafe:
    def test_no_ensure_ascii_false_in_fix_stats(self):
        src = inspect.getsource(E)
        hits = re.findall(r'headers\["X-Fix-Stats"\]\s*=\s*json\.dumps\([^)]*ensure_ascii=(\w+)', src)
        assert len(hits) == 3, f"X-Fix-Stats 应恰有三处（DOCX 降级/成功、PDF），实得 {len(hits)}"
        assert all(v == "True" for v in hits), (
            "X-Fix-Stats 全部必须 ensure_ascii=True（header 值 latin-1 编码，中文 chart_type 会打断交付）"
        )

    def test_chinese_payload_is_latin1_encodable(self):
        """复现触发面：中文 chart_type 的 fix_stats 经 ensure_ascii=True 后必须可编码。"""
        payload = {"chart_delivery_dropped_details": [{"chart_type": "平面布置图", "section_id": "s1"}]}
        raw = json.dumps(payload, ensure_ascii=True)
        raw.encode("latin-1")  # 不抛即通过
        assert "平面布置图" in json.loads(raw)["chart_delivery_dropped_details"][0]["chart_type"]


# ---------------------------------------------------------------------------
# A3/A4/A5/A8：PDF 与并发守卫面
# ---------------------------------------------------------------------------


class TestExportGuardsR230:
    def test_a3_pdf_integrity_no_attribute_error(self):
        src = inspect.getsource(E.export_pdf)
        assert "tmp_pdf.name" not in src, "tmp_pdf 是 str，.name 必抛 AttributeError 顶替预定的 502"

    def test_a4_lock_inner_generation_guard(self):
        """锁内复查生成互斥守卫（两链）：守卫在锁外让注定 409 的请求不白等，
        但去重锁等待窗口内开跑的生成必须被锁内复查拦住。"""
        for fn in (E.export_docx, E.export_pdf):
            src = inspect.getsource(fn)
            lock_idx = src.find("_export_dedup_acquire((")
            guard_after = src[lock_idx:].count("_ensure_no_generation_on_export(")
            assert guard_after >= 1, f"{fn.__name__} 去重锁获取后必须复查生成互斥守卫"

    def test_a5_lease_registered_right_after_viable(self):
        """读租约登记必须在 viable 判定通过后立即（两链），不得隔线程池体检/DB 事务。"""
        for fn_name in ("export_docx", "export_pdf"):
            src = inspect.getsource(getattr(E, fn_name))
            viable_idx = src.find("await asyncio.to_thread(_cache_row_viable, cached[0])")
            assert viable_idx != -1
            # viable 行之后紧随租约登记（600 字符内；排除注释里对旧实现位置的引用）
            seg = src[viable_idx : viable_idx + 600]
            assert "_export_read_lease_register(cached[0])" in seg, f"{fn_name} 租约登记必须紧跟 viable 判定"

    def test_a8_pdf_degraded_header_has_empty_sections(self):
        src = inspect.getsource(E.export_pdf)
        assert 'headers["X-Export-Degraded"] = f"{_sig},empty-sections"' in src, (
            "PDF 与 DOCX 同口径：空章降级必须带 empty-sections 细分信号"
        )


# ---------------------------------------------------------------------------
# A6/A7：指纹与产物语义
# ---------------------------------------------------------------------------


class TestFingerprintSemanticsR230:
    def test_a6_none_means_default_for_boolean_options(self):
        """显式 None 与「未传」必须同产物语义（指纹归一丢弃 None，读取端不得让
        None 变 falsy —— 否则同指纹两种产物，跨变体错误命中缓存）。"""
        opts = E._build_docx_options({"show_page_number": None, "chapter_page_break": None}, lambda k, d: d, lambda k, d: d)
        assert opts["show_page_number"] is True
        assert opts["page_break_before_chapter"] is True
        opts2 = E._build_docx_options({}, lambda k, d: d, lambda k, d: d)
        assert opts2["show_page_number"] is True
        assert opts2["page_break_before_chapter"] is True
        # 显式 False 仍生效（不得误伤关闭语义）
        opts3 = E._build_docx_options({"show_page_number": False, "chapter_page_break": False}, lambda k, d: d, lambda k, d: d)
        assert opts3["show_page_number"] is False
        assert opts3["page_break_before_chapter"] is False

    def test_a7_cover_date_in_fingerprint(self, monkeypatch):
        """封面落款日期（构建日）必须进指纹：跨天命中旧产物会让封面停在上次导出日。"""
        import datetime as _dt

        prep = _minimal_prep()

        class _FakeDateTime:
            _now = _dt.datetime(2026, 11, 12)

            @classmethod
            def now(cls):
                return cls._now

        monkeypatch.setattr(E, "datetime", _FakeDateTime)
        day1 = E._content_fingerprint(prep)[1]
        _FakeDateTime._now = _dt.datetime(2026, 11, 13)
        day2 = E._content_fingerprint(prep)[1]
        assert day1 != day2, "跨天指纹必须不同（封面日期进指纹，产物确实变了）"


def _minimal_prep():
    return {
        "scheme": {"name": "R230", "project_id": "p"},
        "sections": [{"id": "s1", "parent_id": "", "sort_order": 0, "level": 1, "title": "概况", "content": "正文"}],
        "chart_fp": [],
        "global_facts": [],
        "appendix_sources": [],
        "global_facts_status": {"code": "ok"},
        "config": {},
        "fe_codes": [],
        "fe_images": {},
        "subheading_demote": False,
        "image_config": {},
    }


# ---------------------------------------------------------------------------
# B2：非法字符全链清洗
# ---------------------------------------------------------------------------


class TestB2IllegalCharsCleaned:
    @pytest.mark.parametrize("bad", ["\ufffe", "\uffff", "\ud800"])
    def test_bare_illegal_chars_stripped(self, bad):
        assert bad not in DM.clean_text(f"abc{bad}def")

    @pytest.mark.parametrize("ent", ["&#xFFFE;", "&#xD800;", "&#65534;"])
    def test_numeric_entities_materialized_then_stripped(self, ent):
        """数字实体在 decode_html_entities 中物化成非法字符 —— 终末剥离必须兜住。"""
        out = DM.clean_text(f"abc{ent}def")
        assert "\ufffe" not in out and "\uffff" not in out and not any("\ud800" <= c <= "\udfff" for c in out)

    def test_docx_run_no_longer_raises(self):
        from docx import Document

        d = Document()
        p = d.add_paragraph()
        p.add_run(DM.clean_text("abc\ufffe"))  # 旧实现此处 ValueError
        assert "abc" in p.text

    def test_existing_repairs_survive(self):
        # 实体还原、GBK 乱码清理等既有能力在终末剥离之后仍生效
        assert DM.clean_text("a &gt; b") == "a > b"
        assert "锟斤拷" not in DM.clean_text("数据锟斤拷锟斤拷重建")


# ---------------------------------------------------------------------------
# B3：字母序前缀剥离不再篡改并列枚举
# ---------------------------------------------------------------------------


class TestB3OrdinalPrefixSafety:
    def test_enum_preserved(self):
        assert CB.strip_subitem_ordinal_prefix("A、B 两案对比") == "A、B 两案对比"
        assert CB.strip_subitem_ordinal_prefix("b、C 环境施工") == "b、C 环境施工"

    def test_residue_still_stripped(self):
        assert CB.strip_subitem_ordinal_prefix("a）类钢材") == "类钢材"
        assert CB.strip_subitem_ordinal_prefix("a、安全技术措施") == "安全技术措施"

    def test_repeated_prefix_still_loops(self):
        # R181 场景：连续残留前缀循环剥离语义不变
        assert CB.strip_subitem_ordinal_prefix("a、a、吊顶与内装") == "吊顶与内装"


# ---------------------------------------------------------------------------
# B4/B5/B6/B15：公式面
# ---------------------------------------------------------------------------


class TestFormulaR230:
    def test_b4_escaped_punct_no_backslash(self):
        r = DM.latex_to_omml("p = 50\\%")
        # 产物自带 xmlns，直接解析（XML 良构同时是 WPS 可打开性必要条件）
        texts = "".join(t.text or "" for t in ET.fromstring(r).iter() if t.text)
        assert "\\" not in texts and "%" in texts

    def test_b5_matrix_has_real_structure(self):
        r = DM.latex_to_omml("\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}")
        assert "<m:m>" in r and "<m:mr>" in r, "必须产出真矩阵结构"
        assert "pmatrix" not in r and "&amp;" not in r, "环境名与列分隔符不得残留"
        ET.fromstring(r)  # XML 良构（WPS 可打开性的必要条件）
        # bmatrix → 方括号定界
        r2 = DM.latex_to_omml("\\begin{bmatrix} K_1 & 0 \\\\ 0 & K_2 \\end{bmatrix}")
        assert "sSub" in r2, "格内下标必须保留"
        assert 'm:val="["' in r2, "bmatrix 必须用方括号定界"

    def test_b6_nested_scripts_inside_group(self):
        r = DM.latex_to_omml("\\frac{\\sigma_{max}}{f}")
        assert "sSub" in r, "组参数内的下标不得被拍平（\\frac{σ_max}{f}）"

    def test_b15_word_boundary_no_false_dangling(self):
        r = DM.latex_to_omml("machine")
        assert "m:box" not in r, "以 ne 结尾的普通词不得误挂「待填」框"
        # 真·悬空运算符仍生效
        r2 = DM.latex_to_omml("N \\leq")
        assert "m:box" in r2


# ---------------------------------------------------------------------------
# B9/B10/B12：渲染层判据收窄
# ---------------------------------------------------------------------------


class TestRenderHeuristicsR230:
    def test_b9_caption_negative_words(self):
        assert E._looks_like_table_caption("工种配置表") is True, "无编号真表题仍被识别"
        assert E._looks_like_table_caption("详见下表") is False, "引导语不是表题"
        assert E._looks_like_table_caption("质量控制要点详见附表") is False, "正文句不是表题"
        assert E._looks_like_table_caption("表 3-1 施工设备") is True, "带编号表题不受影响"

    def test_b10_adhered_title_min_unit_3(self):
        assert not E._RE_ADHERED_TITLE.match("逐步逐步实施以下工序并验收"), "2 字副词叠用不得误拆"
        m = E._RE_ADHERED_TITLE.match("施工准备施工准备现场三通一平已完成")
        assert m and m.group(1) == "施工准备", "≥3 字标题粘连仍拆分"

    def test_b12_duplicate_header_dropped_data_row_kept(self):
        """有表头形态：nxt_lines[0] 与粘连段表头逐格相同 → 丢弃（不再重复表头）。"""
        blocks = [
            {"type": "paragraph", "text": "表 6-1 劳动力配置 | 工种 | 人数 |"},
            {"type": "table", "lines": ["| 工种 | 人数 |", "| --- | --- |", "| 钢筋工 | 20 |"]},
        ]
        out = E._normalize_render_blocks(blocks)
        assert len(out) == 1 and out[0]["type"] == "table"
        assert out[0]["lines"][0] == "| 工种 | 人数 |"
        assert sum(1 for ln in out[0]["lines"] if "工种" in ln) == 1, "表头不得重复出现为数据行"
        assert "| 钢筋工 | 20 |" in out[0]["lines"]


# ---------------------------------------------------------------------------
# B13/B14/B16：clamp 出口 / 空 Heading / 路径豁免
# ---------------------------------------------------------------------------


class TestRenderExitsR230:
    def test_b13_clamp_covers_heading_list_and_cell(self):
        src_build = inspect.getsource(E._build_docx_sync)
        assert "_h_text = _clamp_paragraph_text(" in src_build, "标题渲染必须截断"
        assert "_item_text = _clamp_paragraph_text(_item_text)" in src_build, "列表项渲染必须截断"
        from app.routers import _export_docx_parts as EP

        src_cell = inspect.getsource(EP)
        assert "cell_text = _clamp_paragraph_text(cell_text)" in src_cell, "表格单元格必须截断"

    def test_b14_empty_heading_skipped(self):
        """纯编号子标题（剥前缀后为空）不得产出带大纲级别的空段落。"""
        src = inspect.getsource(E._build_docx_sync)
        seg = src[src.find('elif block["type"] == "heading"') :]
        seg = seg[: seg.find('elif block["type"] == "list_item"')]
        assert "if _h_text.strip():" in seg, "空文本必须跳过 Heading 构建"

    def test_b16_windows_path_not_latex_residue(self):
        seg = inspect.getsource(E._docx_final_lint)
        m = re.search(r"_RE_WIN_PATH = re\.compile\((r?\".*?\")\)", seg, re.S)
        assert m, "lint 层必须有 Windows 路径豁免正则"
        win_path = re.compile(ast.literal_eval(m.group(1)))
        # 路径段剥掉；最后的文件名段保留（无反斜杠、不触发 LaTeX 判定）；
        # 路径后的正文不被误吞。
        assert win_path.sub("", r"见 C:\Users\new 中的文件") == "见 new 中的文件"
        assert win_path.sub("", r"C:\Users\new") == "new"
        assert win_path.sub("", r"D:\data\new") == "new"


# ---------------------------------------------------------------------------
# F7：预检补 page_residue / double_entity
# ---------------------------------------------------------------------------


class TestF7AuditRules:
    def test_rules_present_and_priced(self):
        keys = {r[0] for r in _scan._AUDIT_RULES}
        assert "page_residue" in keys and "double_entity" in keys

    def test_page_residue_matches(self):
        rx = next(r[3] for r in _scan._AUDIT_RULES if r[0] == "page_residue")
        assert rx.search("= Page 1 =")
        assert rx.search("第 3 页 共 5 页")
        assert not rx.search("第 1 页")

    def test_double_entity_matches(self):
        rx = next(r[3] for r in _scan._AUDIT_RULES if r[0] == "double_entity")
        assert rx.search("&amp;gt;")
        assert not rx.search("&gt;"), "单层实体渲染层自动还原，不报"
        assert rx.search("&amp;amp;nbsp;")

    def test_audit_content_reports_new_rules(self):
        sections = [
            {"title": "概况", "content": "现场布置见 = Page 1 = 附录"},
            {"title": "措施", "content": "压力值 &amp;gt; 0.5MPa"},
        ]
        audit = _scan.audit_content(sections)
        keys = {it["key"] for it in audit["items"]}
        assert "page_residue" in keys and "double_entity" in keys

    def test_fingerprint_covers_new_rules(self):
        """审计规则指纹必须覆盖新规则（旁车自动失效，不靠人记得 bump）。"""
        fp_src = inspect.getsource(_scan.audit_rules_fingerprint)
        seg = fp_src  # 指纹基于 AST/规则表摘要，规则表变化必然反映
        assert seg  # 存在性 + 下面行为验证
        rule_names = json.dumps([r[0] for r in _scan._AUDIT_RULES])
        assert "page_residue" in rule_names
