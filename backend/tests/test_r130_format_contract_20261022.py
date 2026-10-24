# -*- coding: utf-8 -*-
"""R130 · 正文生成与导出格式统一收口（2026-10-22）回归测试。

覆盖本轮（R130）新增/修改的四块：
1. 三端格式契约模块 content_format_contract —— 六类标准自检器（第一层判据）；
2. 三层校验接线 —— 第一层（sse_handlers 正文生成后自检）、
   第二层（export.audit_content 导出前校验）；
3. 文字标准 —— 标题黑体、西文 Times New Roman（样式级 + run 级）；
4. 图表渲染 —— HTTP 基础设施不可用时 allow_pil=False 也回退 PIL（日志 TOP1
   根因修复），代码语法错（4xx）仍维持不降级。

每条测试在对应修复落地前均应失败。
"""
import io

from PIL import Image

# ============================================================
# 1. 契约自检器（六类标准判据）
# ============================================================

class TestFormatContractChecks:

    def test_heading_missing_space_is_error(self):
        """文字标准：`#标题` 缺空格 → error（渲染器不会识别为标题）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("#第一节 施工部署\n正文")
        assert rep["checked"] is True
        assert rep["passed"] is False
        assert any(i["category"] == "text" and i["severity"] == "error"
                   for i in rep["issues"]), rep["issues"]

    def test_heading_inline_format_is_warning(self):
        """文字标准：标题内出现 `**加粗**`/行内代码 → warning（应走命名样式）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("## 施工**部署**\n正文")
        assert any(i["category"] == "text" and i["severity"] == "warning"
                   for i in rep["issues"]), rep["issues"]

    def test_manual_list_numbering_is_advisory(self):
        """符号标准：`1、正文` 手工序号 → advisory（建议标准 Markdown 列表）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("1、测量放线\n2、基坑开挖")
        assert any(i["category"] == "symbol" and i["severity"] == "advisory"
                   for i in rep["issues"]), rep["issues"]

    def test_standard_md_list_not_flagged(self):
        """标准 Markdown 列表语法（`- ` / `1.`）不应被报为手工序号。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("- 测量放线\n1. 基坑开挖")
        assert not any(i["category"] == "symbol" for i in rep["issues"]), \
            rep["issues"]

    def test_table_missing_separator_is_error(self):
        """表格标准：GFM 表格缺分隔行 → error（导出会降级为纯文本）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("| a | b |\n| 1 | 2 |")
        assert any(i["category"] == "table" and i["severity"] == "error"
                   for i in rep["issues"]), rep["issues"]

    def test_valid_gfm_table_passes(self):
        """合法 GFM 表格（表头 + 分隔行 + 数据行）不应报表格问题。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(
            "| 部位 | 做法 |\n| --- | --- |\n| 底板 | C35 |")
        assert not any(i["category"] == "table" for i in rep["issues"]), \
            rep["issues"]

    def test_unclosed_fence_is_error(self):
        """图表标准：未闭合围栏 → error（生成截断，导出整块跳过）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("正文\n```mermaid\nflowchart TD\n  A-->B")
        assert any(i["category"] == "chart" and i["severity"] == "error"
                   and "未闭合" in i["message"] for i in rep["issues"]), \
            rep["issues"]

    def test_mermaid_type_outside_whitelist_is_warning(self):
        """图表标准：Mermaid 类型不在契约白名单 → warning。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(
            "正文\n```mermaid\nmindmap\n  root((项目部))\n```\n")
        assert any(i["category"] == "chart" and i["severity"] == "warning"
                   for i in rep["issues"]), rep["issues"]

    def test_chart_json_non_object_is_error(self):
        """图表标准：chart-json 围栏内容非 JSON 对象 → error。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("```chart-json\nnot json\n```\n")
        assert any(i["category"] == "chart" and i["severity"] == "error"
                   for i in rep["issues"]), rep["issues"]

    def test_control_char_and_mojibake_is_error(self):
        """符号标准：控制字符 / GBK 乱码（锟斤拷）→ error（编码问题归符号类）。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("正文\x07继续")
        assert any(i["category"] == "symbol" and i["severity"] == "error"
                   and "控制字符" in i["message"] for i in rep["issues"]), \
            rep["issues"]
        rep2 = check_format_contract("锟斤拷锟斤拷")
        assert any(i["category"] == "symbol" and i["severity"] == "error"
                   for i in rep2["issues"]), rep2["issues"]

    def test_figure_title_format_is_warning(self):
        """数字/编号标准：图题有数字但不符 `图{章号}-{序号} {图名}` → warning。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract("图1 施工流程")
        assert any(i["category"] == "numbering" and i["severity"] == "warning"
                   for i in rep["issues"]), rep["issues"]

    def test_pure_content_passes(self):
        """完全合规的正文应 passed=True、零问题。"""
        from app.services.content_format_contract import check_format_contract

        rep = check_format_contract(
            "## 施工工艺流程\n\n本工程采用流水施工组织方式。\n\n"
            "- 测量放线\n- 基坑开挖\n")
        assert rep["passed"] is True, rep["issues"]
        assert rep["error_count"] == 0

    def test_fail_soft_on_bad_input(self):
        """fail-soft：非字符串/空内容 → checked=False 空报告，不抛异常。"""
        from app.services.content_format_contract import check_format_contract

        for bad in (None, "", 123, b"bytes"):
            rep = check_format_contract(bad)
            assert rep["checked"] is False
            assert rep["issues"] == []


# ============================================================
# 2. 三层校验接线（第一层 sse_handlers / 第二层 audit_content）
# ============================================================

class TestThreeLayerWiring:

    def test_layer1_sse_writes_format_contract_report(self):
        """第一层：正文生成后自检 —— sse_handlers 必须调用 check_format_contract
        并把结果写入 report["format_contract"]（加法式新键）。"""
        import inspect

        from app.routers import sse_handlers
        src = inspect.getsource(sse_handlers)
        assert "check_format_contract(" in src, \
            "sse_handlers 未接线第一层格式契约自检"
        assert 'report["format_contract"]' in src, \
            "自检结果未写入 report[format_contract]"

    def test_layer2_audit_content_includes_contract_items(self):
        """第二层：导出前校验 —— audit_content 必须把契约问题并入体检清单
        （key=format_contract_*），且不与 unclosed_fence 重复计数。"""
        from app.routers.export import audit_content

        sections = [{"id": "c1", "title": "第一章",
                     "content": "#缺空格标题\n正文"}]
        audit = audit_content(sections)
        keys = [it["key"] for it in audit["items"]]
        assert any(k.startswith("format_contract_") for k in keys), keys

        # unclosed fence 同一事实不得被契约二次计数
        sec2 = [{"id": "c2", "title": "第二章",
                 "content": "```mermaid\nflowchart TD\n  A-->B"}]
        audit2 = audit_content(sec2)
        fc_chart = [it for it in audit2["items"]
                    if it["key"] == "format_contract_chart"]
        unclosed = [it for it in audit2["items"]
                    if it["key"] == "unclosed_fence"]
        assert unclosed and unclosed[0]["count"] == 1, audit2["items"]
        # 契约图表槽不得再含「代码块未闭合」这条（已由 unclosed_fence 计数）
        assert not fc_chart, \
            f"契约与 unclosed_fence 重复计数：{fc_chart}"

    def test_layer2_is_read_only(self):
        """第二层只体检不篡改：调用后正文内容不得变化。"""
        from app.routers.export import audit_content

        content = "#缺空格\n| a | b |\n| 1 | 2 |"
        sections = [{"id": "c1", "title": "章", "content": content}]
        audit_content(sections)
        assert sections[0]["content"] == content


# ============================================================
# 3. 文字标准（标题黑体 / 西文 Times New Roman）
# ============================================================

class TestTextStandard:

    def test_default_heading_font_is_heiti(self):
        """文字标准：默认 1~9 级标题中文字体必须是黑体（标题黑体）。"""
        from app.routers.export import _DEFAULT_HEADING_STYLES

        for lvl in range(1, 10):
            assert _DEFAULT_HEADING_STYLES[lvl]["font_name"] == "黑体", \
                f"第 {lvl} 级标题默认字体不是黑体"

    def test_latin_font_constant(self):
        """西文统一 Times New Roman（三端唯一事实源）。"""
        from app.services.content_format_contract import FONT_LATIN

        assert FONT_LATIN == "Times New Roman"

    def test_cjk_font_detection(self):
        """CJK 字体名识别：中文字体名 → True，西文字体名 → False。"""
        from app.routers.export import _is_cjk_font

        assert _is_cjk_font("宋体") is True
        assert _is_cjk_font("黑体") is True
        assert _is_cjk_font("微软雅黑") is True
        assert _is_cjk_font("Consolas") is False
        assert _is_cjk_font("Times New Roman") is False
        assert _is_cjk_font("") is False

    def test_run_font_splits_latin_slot_for_cjk_name(self):
        """run 级字体：中文字体名只进 w:eastAsia，w:ascii/w:hAnsi 写 TNR。"""
        from app.routers.export import _set_run_font
        from app.services.content_format_contract import FONT_LATIN
        from docx import Document
        from docx.oxml.ns import qn

        doc = Document()
        p = doc.add_paragraph()
        _set_run_font(p.add_run("混凝土 28d 强度"), "仿宋", 12)
        r = p.runs[0]._element
        rfonts = r.get_or_add_rPr().get_or_add_rFonts()
        assert rfonts.get(qn("w:ascii")) == FONT_LATIN
        assert rfonts.get(qn("w:hAnsi")) == FONT_LATIN
        assert rfonts.get(qn("w:eastAsia")) == "仿宋"

        # 西文字体（行内代码）行为不变：三槽同值
        p2 = doc.add_paragraph()
        _set_run_font(p2.add_run("print()"), "Consolas")
        rfonts2 = p2.runs[0]._element.get_or_add_rPr().get_or_add_rFonts()
        assert rfonts2.get(qn("w:ascii")) == "Consolas"
        assert rfonts2.get(qn("w:eastAsia")) == "Consolas"


# ============================================================
# 4. 图表渲染：基础设施回退（日志 TOP1 根因修复）
# ============================================================

class TestMermaidInfraFallback:

    @staticmethod
    def _fake_client(exc):
        from app.services.ai.mermaid_service import MermaidRenderError

        class _Client:
            async def render_mermaid_via_http(self, code, theme="neutral",
                                              scale=2):
                if isinstance(exc, MermaidRenderError):
                    raise exc
                raise exc

        return _Client()

    def test_infra_failure_falls_back_to_pil_even_when_disallow(self,
                                                                monkeypatch):
        """回归：HTTP Service「无可用后端」（基础设施不可用）时，
        allow_pil=False（导出默认）也必须回退 PIL v2 出图 ——
        旧实现直接 return None，全部 Mermaid 图渲染失败、跳过不占号。"""
        import app.services.ai.mermaid_renderer as mr
        from app.services.ai.mermaid_service import MermaidRenderError

        exc = MermaidRenderError(
            "所有渲染后端均不可用: 无可用后端", backend="http")
        monkeypatch.setattr(mr, "get_mermaid_service_client",
                            lambda *a, **k: self._fake_client(exc))
        res = mr.render_mermaid_to_bytes(
            "flowchart TD\n    A[开始] --> B[结束]",
            "flowchart", skip_http=False, allow_pil=False)
        assert res is not None, "基础设施不可用时未回退 PIL（成稿将缺图）"
        assert len(res.getvalue()) > 100

    def test_content_error_still_disallows_fallback(self, monkeypatch):
        """代码语法错（4xx）≠ 基础设施不可用：allow_pil=False 仍返回 None
    （由调用方写占位提示，不把语法错掩盖成降级图）。"""
        import app.services.ai.mermaid_renderer as mr
        from app.services.ai.mermaid_service import MermaidRenderError

        exc = MermaidRenderError("Parse error on line 1", backend="http",
                                 status_code=422)
        monkeypatch.setattr(mr, "get_mermaid_service_client",
                            lambda *a, **k: self._fake_client(exc))
        # 载荷刻意与基础设施用例不同，避免命中进程内渲染缓存
        res = mr.render_mermaid_to_bytes(
            "flowchart TD\n    A[开挖] --> B[支护]",
            "flowchart", skip_http=False, allow_pil=False)
        assert res is None, "语法错应维持不降级（返回 None）"

    def test_connection_error_treated_as_infrastructure(self, monkeypatch):
        """网络连接异常（非 MermaidRenderError）同样视为基础设施不可用。"""
        import app.services.ai.mermaid_renderer as mr

        monkeypatch.setattr(
            mr, "get_mermaid_service_client",
            lambda *a, **k: self._fake_client(ConnectionError("refused")))
        res = mr.render_mermaid_to_bytes(
            "flowchart TD\n    A --> B", "flowchart",
            skip_http=False, allow_pil=False)
        assert res is not None, "连接失败未按基础设施不可用回退 PIL"


# ============================================================
# 5. 图表质量：分辨率 ≥150dpi 告警
# ============================================================

class TestImageDpiCheck:

    def test_low_dpi_image_logs_warning(self, caplog):
        """质量要求「分辨率≥150dpi」：低清图入稿必须打 WARNING（fail-soft）。"""
        import logging

        from app.routers.export import _insert_image_with_caption
        from app.services.content_format_contract import MIN_IMAGE_DPI
        from docx import Document

        assert MIN_IMAGE_DPI == 150
        buf = io.BytesIO()
        # 80px 宽小图 → 入稿 ~2cm，有效 dpi 远低于 150
        Image.new("RGB", (80, 80), "white").save(buf, format="PNG")

        doc = Document()
        with caplog.at_level(logging.WARNING,
                             logger="app.routers.export"):
            ok = _insert_image_with_caption(
                doc, buf, "图 1-1 测试图", log_label="AI 配图(图 1-1)")
        assert ok is True  # fail-soft：只告警不阻断
        assert any("dpi" in r.getMessage().lower()
                   or "分辨率" in r.getMessage()
                   for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
