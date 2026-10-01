"""2026-09-27 全模块深度审查 · P0/P1 缺陷修复回归护栏（第一批：导出链）。

覆盖本轮修复的导出侧缺陷，每条含「正向 + 反例」双向断言：

| 组 | 缺陷 | 修复位置 |
|---|---|---|
| T1 | 全局事实附录整块丢失（`_set_run_font` 位置参数 TypeError 被宽泛 except 吞掉） | export.py 附录渲染 |
| T2 | 标题主题字体反覆盖显式字体（w:asciiTheme 优先于 w:ascii） | export.py 样式构建 |
| T3 | 无 L1 时图号/表号命名空间塌缩到 ch1 | export.py `_figure_chapter_num` |
| T7 | 缺图文档被写入缓存（坏缓存守卫失效） | export.py `ai_image_pending` |

护栏原则：反例必须**真的能跑通修复前的旧路径**，否则测试只是自证。
"""
import asyncio
import inspect
import zipfile
from io import BytesIO

import pytest

from app.services.docx_math import clean_text, strip_table_markup


def _png_bytes(size: int = 400) -> bytes:
    """构造一个最小合法 PNG（够 _chart_ok 的 >100 字节判据）。"""
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (size, size), "white").save(buf, format="PNG")
    return buf.getvalue()


def _appendix_docx(tmp_path, global_facts, sections=None, name="appendix.docx"):
    """用 _build_docx_sync 渲染一份（可带全局事实附录的）docx，返回文件路径。"""
    from app.routers.export import _build_docx_sync, _load_heading_styles
    from app.services.content_blocks import _parse_content_blocks

    if sections is None:
        sections = [
            {"id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
             "title": "第一章 总体概述", "content": "## 总体安排\n本章正文。\n"},
        ]
    out = str(tmp_path / name)
    _build_docx_sync(
        out,
        {"id": "s1", "project_id": "p1", "name": "深基坑开挖专项施工方案"},
        sections, {}, {}, {},
        {s["id"]: _parse_content_blocks(s["content"]) for s in sections},
        "宋体", 12, "", "", True, False, False,
        bidder_name="",
        heading_styles=_load_heading_styles({}),
        line_spacing=1.5,
        page_number_style="simple",
        toc_depth=3,
        global_facts=global_facts,
    )
    return out

# ============================================================
# F1（2026-09-27 · P0）规则注册表一致性：唯一事实源必须可验证
# ============================================================
class TestRuleRegistryConsistency:
    """✅ BUG 修复（2026-09-27 · P0）：审核规则「唯一事实源」此前无人校验。

    已发生两处真实分叉：
    1. ``DLV-13`` / ``DLV-14`` 由 ``export.py`` 产出却从未注册 → high 级问题
       落到兜底分支，title 退化为「导出预检问题」、basis/suggestion 全空；
    2. ``CON-05-1`` 等派生编号使 ``get_rule`` 返回 None → dimension 退化为空串，
       consistency 维度的扣分被记到 deliverability（权重 15→10），总分系统性偏移。
    """

    def test_registry_is_healthy(self):
        """当前注册表必须零问题（含维度/严重度/方式/依据/外部映射/引擎产出）。"""
        from app.routers.export import _EXPORT_ISSUE_RULE_MAP
        from app.services.audit_rules import _PROGRAM_EMITTED_RULE_IDS, validate_rule_registry
        problems = validate_rule_registry(
            mapping=_EXPORT_ISSUE_RULE_MAP,
            emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS,
        )
        assert problems == []

    def test_dlv13_dlv14_are_registered_with_real_metadata(self):
        """反例回归：这两条必须有可用的标题/依据/严重度（而非兜底占位）。"""
        from app.services.audit_rules import get_rule
        for rid, sev in (("DLV-13", "high"), ("DLV-14", "medium")):
            rule = get_rule(rid)
            assert rule is not None, f"{rid} 未注册"
            assert rule.dimension == "deliverability"
            assert rule.severity == sev
            assert rule.title and rule.title != rid, f"{rid} 标题退化为 ID 本身"
            assert rule.basis, f"{rid} 缺少行业依据"

    def test_export_issue_map_targets_all_registered(self):
        """导出预检映射表引用的每个 rule_id 都必须已注册。"""
        from app.routers.export import _EXPORT_ISSUE_RULE_MAP
        from app.services.audit_rules import get_rule
        for issue_type, (rid, _sev) in _EXPORT_ISSUE_RULE_MAP.items():
            assert get_rule(rid) is not None, \
                f"issue 类型 {issue_type!r} 映射到未注册规则 {rid}"

    def test_derived_rule_ids_resolve_to_base_rule(self):
        """派生编号必须能回退到基规则（否则 dimension 塌陷为空串）。"""
        from app.services.preflight_engine import _resolve_rule
        assert _resolve_rule("CON-05-1").rule_id == "CON-05"
        assert _resolve_rule("CON-05-999").rule_id == "CON-05"
        assert _resolve_rule("CON-05").rule_id == "CON-05"
        assert _resolve_rule("BOGUS-1") is None
        # 不得误伤非「-<数字>」后缀的未知 ID
        assert _resolve_rule("CON-05-EXTRA") is None

    def test_derived_finding_keeps_full_dimension(self):
        """核心护栏：CON-05-N 产出的 finding 必须带真实 dimension（回归 P0）。"""
        from app.services.preflight_engine import _finding
        f = _finding("CON-05-1", "重复内容", suggestion="合并章节")
        assert f["rule_id"] == "CON-05-1", "rule_id 须保留派生值（去重语义依赖它）"
        assert f["dimension"] == "consistency", "维度塌陷为空串会让扣分记错维度"
        assert f["title"] and f["title"] != "CON-05-1"
        assert f["basis"], "high 级阻断项不得没有行业依据"

    def test_scoring_uses_correct_dimension_for_duplicates(self):
        """端到端：查重扣分必须落在 consistency 维度（而非 deliverability）。"""
        from app.services.audit_scoring import score_findings
        from app.services.preflight_engine import _finding
        result = score_findings([_finding("CON-05-1", "重复", suggestion="s")])
        assert result.unknown_dimension_count == 0, "派生编号被计入未知维度"
        scores = {d.key: d.score for d in result.dimensions}
        assert scores.get("consistency", 100.0) < 100.0, \
            "查重问题未计入 consistency 维度"
        assert scores.get("deliverability", 100.0) == 100.0, \
            "查重扣分被错误记到 deliverability 维度（派生编号维度塌陷的典型症状）"

    def test_validate_detects_missing_registration(self):
        """护栏：自检函数必须真能发现漏登（对修复前的状态敏感）。"""
        import app.services.audit_rules as ar
        from app.routers.export import _EXPORT_ISSUE_RULE_MAP
        saved_rules, saved_map = ar.ALL_RULES, ar.RULE_MAP
        try:
            ar.ALL_RULES = tuple(r for r in saved_rules
                                 if r.rule_id not in ("DLV-13", "DLV-14"))
            ar.RULE_MAP = {r.rule_id: r for r in ar.ALL_RULES}
            problems = ar.validate_rule_registry(mapping=_EXPORT_ISSUE_RULE_MAP)
            assert len(problems) >= 2, "漏登 DLV-13/14 未被自检发现"
            assert any("DLV-13" in p for p in problems)
            assert any("DLV-14" in p for p in problems)
        finally:
            ar.ALL_RULES, ar.RULE_MAP = saved_rules, saved_map
        assert ar.validate_rule_registry(mapping=_EXPORT_ISSUE_RULE_MAP) == []

    def test_all_rules_have_basis_and_title(self):
        """维护约定：basis 必须可核实，title 面向用户。"""
        from app.services.audit_rules import active_rules
        for r in active_rules():
            assert r.title, f"{r.rule_id} 缺标题"
            assert r.basis, f"{r.rule_id} 缺行业依据"
            assert r.severity in ("low", "medium", "high", "block")

    def test_rule_version_bumped_after_new_rules(self):
        """规则增删必须同步 RULE_VERSION（历史结果才可比对）。"""
        from app.services.audit_rules import RULE_VERSION
        assert tuple(int(x) for x in RULE_VERSION.split(".")[:2]) >= (1, 6), \
            f"新增 DLV-13/DLV-14 后必须 bump RULE_VERSION，当前 {RULE_VERSION}"




# ============================================================
# E1（2026-09-27 · P0）路由契约：装饰器不得挂错函数
# ============================================================
class TestExportRouteRegistration:
    """✅ BUG 修复（2026-09-27 · P0）：``@router.post("/docx")`` 曾错贴在辅助函数
    ``_summarize_numbering_consistency`` 上，真正的 ``export_docx`` **没有任何装饰器**。

    线上后果：``POST /api/v1/schemes/{id}/export/docx`` 实际返回
    ``"不一致章节数=0"`` 这一 9 字节 JSON 字符串（content-type: application/json），
    而前端 ``exportApi.docx`` 以 ``responseType:"blob"`` 接收 → 用户下载到一个
    打不开的 9 字节「.docx」，**DOCX 导出功能整体失效**。

    之所以 2900+ 用例全绿：既有导出测试一律 ``await export_docx("s1", body, db=db)``
    **直接调函数绕过路由表**，断言的是函数行为而非 HTTP 行为。故本类改从
    **路由注册表**这一唯一事实源断言，任何装饰器错位都会立刻暴露。
    """

    ROUTER = "app.routers.export.router"

    def _routes(self):
        from app.routers import export
        return export.router.routes

    def test_docx_route_binds_the_real_export_function(self):
        """docx 端点必须绑定 export_docx（核心功能入口）。"""
        hits = [r for r in self._routes() if r.path.endswith("/export/docx")]
        assert len(hits) == 1, f"/export/docx 应恰好注册 1 条，实际 {len(hits)} 条"
        assert hits[0].endpoint.__name__ == "export_docx"
        assert "POST" in hits[0].methods

    def test_pdf_route_binds_the_real_export_function(self):
        """pdf 端点绑定 export_pdf（对照组：证明断言不是恒真）。"""
        hits = [r for r in self._routes() if r.path.endswith("/export/pdf")]
        assert len(hits) == 1
        assert hits[0].endpoint.__name__ == "export_pdf"
        assert "POST" in hits[0].methods

    def test_no_helper_is_exposed_as_endpoint(self):
        """反例回归：内部辅助函数不得出现在路由表里（装饰器错位的通用形态）。"""
        endpoint_names = {r.endpoint.__name__ for r in self._routes()}
        for helper in ("_summarize_numbering_consistency", "_prepare_export",
                       "_content_fingerprint", "_normalize_config",
                       "_build_docx_sync", "_build_docx_task", "_export_issues_to_findings"):
            assert helper not in endpoint_names, f"辅助函数 {helper} 被误注册为路由端点"

    def test_docx_and_pdf_return_binary_not_json(self):
        """回归缺陷本体：docx 端点必须返回二进制文档，而不是 JSON 字符串。

        直接复现原始 BUG 的线上表现（错位时返回 "不一致章节数=0" 的 JSON）。
        """
        from app.routers.export import export_docx, export_pdf

        for fn in (export_docx, export_pdf):
            # 端点签名为 (scheme_id, body, db) 且是协程：装饰器正确时 FastAPI
            # 才会把它当作真实端点（而非把辅助函数的 dict 参数当查询参数）。
            sig = inspect.signature(fn)
            assert list(sig.parameters)[:2] == ["scheme_id", "body"], \
                f"{fn.__name__} 签名异常：{list(sig.parameters)}"
            assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为 async 端点"

    def test_export_router_included_in_app(self):
        """路由表须真正挂进 app（防止 include_router 被误删）。

        注意：本仓所用 FastAPI 版本把 ``include_router`` 的结果包成 ``_IncludedRouter``
        占位对象，**不展平**进 ``app.routes``（实测 app.routes 只有 25 项且 path 为空串）。
        故此处以 ``app.openapi()["paths"]`` 为准 —— 它是 FastAPI 自己展平后的权威视图，
        任何装饰器错位 / 路由漏挂都会在此暴露。
        """
        from app.main import app
        paths = app.openapi()["paths"]
        docx = [p for p in paths if p.endswith("/export/docx")]
        pdf = [p for p in paths if p.endswith("/export/pdf")]
        assert len(docx) == 1, f"OpenAPI 中 /export/docx 数量异常：{docx}"
        assert len(pdf) == 1, f"OpenAPI 中 /export/pdf 数量异常：{pdf}"
        assert "post" in paths[docx[0]], "docx 端点必须是 POST"
        assert "post" in paths[pdf[0]], "pdf 端点必须是 POST"

    def test_docx_declares_request_body(self):
        """docx 端点须接收 body（config + chart_images），不得是无参 GET 式端点。

        回归缺陷本体的另一侧面：装饰器错位时 ``_summarize_numbering_consistency(report)``
        的 ``report`` 被 FastAPI 当成**查询参数**，OpenAPI 里既无 body 也无 requestBody。
        """
        from app.main import app
        paths = app.openapi()["paths"]
        docx = next(p for p in paths if p.endswith("/export/docx"))
        op = paths[docx]["post"]
        assert "requestBody" in op, "docx 端点缺少 requestBody（装饰器可能错位）"
        rb = op["requestBody"]
        assert "application/json" in rb.get("content", {}), \
            f"docx 端点未声明 JSON body，实际：{list(rb.get('content', {}))}"


class TestImageFormatDocxCompatibility:
    """✅ BUG 修复（2026-09-27 · P1）：docx 不支持的图片格式不得占用图号。

    背景：``_chart_ok`` 原先只验「是不是图片 + 长度 > 100」，不验「docx 能不能插入」。
    实测 WEBP 通过校验 → 图号已被自增占用 → ``doc.add_picture`` 抛
    ``UnrecognizedImageError`` 被 except 吞掉 → 成稿只剩孤立的「图 1-1 …」图题，
    且该坏产物还会被写入 export_cache 永久命中。
    """

    @staticmethod
    def _img(fmt: str, size=(300, 300)) -> BytesIO:
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", size, "white").save(buf, format=fmt)
        buf.seek(0)
        return buf

    def test_webp_is_rejected(self):
        from app.routers.export import _chart_ok
        assert _chart_ok(self._img("WEBP")) is False

    def test_png_jpeg_accepted(self):
        from app.routers.export import _chart_ok
        assert _chart_ok(self._img("PNG")) is True
        assert _chart_ok(self._img("JPEG")) is True

    def test_rejection_matches_docx_actual_capability(self):
        """核心护栏：判定口径必须与 python-docx 的真实能力一致，而非拍脑袋白名单。"""
        from docx import Document
        from docx.shared import Inches
        from app.routers.export import _chart_ok
        for fmt in ("WEBP", "PNG", "JPEG", "GIF", "BMP"):
            img = self._img(fmt)
            payload = img.getvalue()
            doc_ok = True
            try:
                Document().add_picture(BytesIO(payload), width=Inches(1))
            except Exception:
                doc_ok = False
            assert _chart_ok(BytesIO(payload)) is doc_ok, \
                f"{fmt}: _chart_ok 与 docx 实际能力不一致"

    def test_empty_and_garbage_still_rejected(self):
        from app.routers.export import _chart_ok
        assert _chart_ok(BytesIO(b"")) is False
        assert _chart_ok(BytesIO(b"x" * 500)) is False
        assert _chart_ok(None) is False

    def test_illustration_path_also_guarded(self):
        """AI 配图插入路径须复用同一格式守卫（否则配图分支仍会占号丢图）。"""
        import inspect
        from app.routers import export
        src = inspect.getsource(export._add_illustration_from_bytes)
        assert "_image_format_supported" in src, \
            "配图插入未复用格式白名单，WEBP 配图仍会占号丢图"

    def test_undersized_still_rejected(self):
        """极小 PNG（< 100 字节）沿用旧的长度门槛。"""
        from PIL import Image
        from app.routers.export import _chart_ok
        buf = BytesIO()
        Image.new("RGB", (2, 2), "white").save(buf, format="PNG")
        small = buf.getvalue()
        if len(small) <= 100:
            assert _chart_ok(BytesIO(small)) is False


class TestImageGenerationParamsInFingerprint:
    """✅ BUG 修复（2026-09-27 · P1）：换配图模型/尺寸必须让导出缓存失效。"""

    def _prep(self):
        return {
            "config": {}, "fe_codes": [], "chart_fp": [], "global_facts": [],
            "global_facts_status": {},
            "scheme": {"name": "方案", "project_id": "p"},
            "sections": [{"id": "c1", "parent_id": "", "sort_order": 0,
                          "level": 1, "title": "第一章", "content": "正文"}],
        }

    def test_signature_excludes_api_key(self):
        """护栏：签名绝不含密钥（会被回显/落库的指纹材料不能带密文）。"""
        from app.routers.export import _image_generation_signature
        sig = _image_generation_signature()
        assert isinstance(sig, dict)
        for k in sig:
            assert "key" not in k.lower(), f"签名不得包含密钥字段：{k}"

    def test_changing_model_changes_content_fingerprint(self):
        from app.routers import export
        prep = self._prep()
        before = export._content_fingerprint(prep)[1]
        orig = export._image_generation_signature
        try:
            export._image_generation_signature = lambda: {
                "enabled": True, "model": "model-A", "size": "1024x1024", "base_url": "u"}
            after = export._content_fingerprint(prep)[1]
            export._image_generation_signature = lambda: {
                "enabled": True, "model": "model-B", "size": "1024x1024", "base_url": "u"}
            after2 = export._content_fingerprint(prep)[1]
        finally:
            export._image_generation_signature = orig
        assert before != after, "换模型后 content_fingerprint 未变化（缓存不会失效）"
        assert after != after2, "模型 A 与 B 的指纹相同（签名未真正参与指纹）"

    def test_fingerprint_still_stable_for_same_inputs(self):
        """护栏：同输入必须同指纹（否则缓存永远不命中，属回归）。"""
        from app.routers import export
        prep = self._prep()
        a = export._content_fingerprint(prep)[1]
        b = export._content_fingerprint(prep)[1]
        assert a == b


# ============================================================
# T1 全局事实附录：不得整块丢失
# ============================================================

class TestGlobalFactsAppendixNotLost:
    """T1：附录渲染里的编程错误不得被降级成「静默跳过」。

    根因：`_set_run_font(run, name, size=None, *, bold=None, ...)` 的 bold 是
    keyword-only，而原代码写了 `_set_run_font(..., 12, True)` —— TypeError 被
    紧邻的 `except Exception` 吞掉，整张「项目关键事实」表消失，导出仍报成功。
    """

    def test_appendix_table_is_actually_rendered(self, tmp_path):
        facts = [
            {"gt": "工程概况", "title": "基坑深度", "content": "- 基坑深度：12.5m"},
            {"gt": "工程概况", "title": "总工期", "content": "- 总工期：90 天"},
        ]
        path = _appendix_docx(tmp_path, facts)
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        # ⚠️ 断言写法：不要用 `assert "..." in xml`（document.xml 是大字符串，
        #    断言失败时 pytest 的 difflib repr 会做 O(n^2) 比对而**卡死测试进程**）。
        missing = [k for k in ("附录：项目关键事实", "工程概况", "基坑深度", "12.5m")
                   if k not in xml]
        assert missing == [], f"附录内容丢失：{missing}"

    def test_no_facts_means_no_appendix(self, tmp_path):
        """反例：没有事实时不应凭空造出附录标题。"""
        path = _appendix_docx(tmp_path, [])
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        assert ("附录：项目关键事实" not in xml) is True

    def test_set_run_font_bold_is_keyword_only(self):
        """根因护栏：锁定 bold 为 keyword-only（`size` 才是位置参数）。"""
        from app.routers.export import _set_run_font
        sig = inspect.signature(_set_run_font)
        assert sig.parameters["bold"].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["size"].kind is not inspect.Parameter.KEYWORD_ONLY

    def test_type_error_branch_logs_error_not_warning(self):
        """反例回归：TypeError 属编程错误，必须 ERROR 上报（而非 WARNING 静默）。"""
        src = inspect.getsource(__import__("app.routers.export", fromlist=["x"]))
        # 附录渲染段必须先捕获 TypeError 再捕获 Exception
        assert "except TypeError" in src
        assert "渲染全局事实附录遇到编程错误" in src

    def test_positional_bold_would_raise(self):
        """反例：确认旧写法确实抛 TypeError（证明本组用例不是自证）。"""
        def _set_run_font(run, name, size=None, *, bold=None):
            return None
        with pytest.raises(TypeError):
            _set_run_font(None, "宋体", 12, True)


# ============================================================
# T2 标题主题字体不得反覆盖显式字体
# ============================================================

class TestHeadingThemeFontCleared:
    """T2：python-docx 内置 Heading 样式带 w:asciiTheme，OOXML 规则下
    Theme 属性**优先于**显式 w:ascii —— 不删除就等于用户的标题字体配置静默失效。

    ⚠️ 断言范围：只检查**文档实际使用的样式**（Normal + Heading 1~7）。
    styles.xml 里另有 60+ 处 `w:asciiTheme`，全部位于 `<w:latentStyles>`
    与未被引用的内置样式（Heading 8/9、TOC 1~9 等）的定义里 —— 它们不参与
    渲染，清除它们既无必要也会污染 python-docx 的内置模板。
    """

    _THEME_ATTRS = ("w:asciiTheme", "w:hAnsiTheme",
                    "w:eastAsiaTheme", "w:cstheme")

    @staticmethod
    def _used_style_theme_attrs(path):
        """返回 {样式名: {主题属性: 值}}，只含实际使用的 Normal/Heading 1~7。"""
        from docx import Document
        from docx.oxml.ns import qn
        doc = Document(path)
        out = {}
        for name in ["Normal"] + [f"Heading {i}" for i in range(1, 8)]:
            rfonts = doc.styles[name].element.get_or_add_rPr().get_or_add_rFonts()
            found = {}
            for attr in TestHeadingThemeFontCleared._THEME_ATTRS:
                v = rfonts.get(qn(attr))
                if v is not None:
                    found[attr] = v
            out[name] = found
        return out

    def test_used_styles_have_no_theme_font_attrs(self, tmp_path):
        path = _appendix_docx(tmp_path, [])
        offenders = {k: v for k, v in self._used_style_theme_attrs(path).items() if v}
        # 用 assert offenders == {} 而不是 `not in xml`：styles.xml 有 350KB，
        # 断言失败时 pytest 的 difflib repr 会做 O(n^2) 文本比对而**卡死整个测试进程**。
        assert offenders == {}, f"以下样式仍带主题字体属性：{offenders}"

    def test_explicit_font_name_preserved(self, tmp_path):
        """反例：不得「删过头」——显式字体必须仍然在。"""
        from docx import Document
        doc = Document(_appendix_docx(tmp_path, []))
        got = {name: doc.styles[name].font.name
               for name in ["Normal"] + [f"Heading {i}" for i in range(1, 8)]}
        assert set(got.values()) == {"宋体"}, f"显式字体被破坏：{got}"

    def test_heading_styles_are_not_italic(self, tmp_path):
        """需求「标题字体不倾斜」：Heading 1~7 必须显式 italic=False。"""
        from docx import Document
        doc = Document(_appendix_docx(tmp_path, []))
        italic = {f"Heading {i}": doc.styles[f"Heading {i}"].font.italic
                  for i in range(1, 8)}
        assert set(italic.values()) == {False}, f"标题出现倾斜：{italic}"


# ============================================================
# T3 无 L1 时图号/表号命名空间不得塌缩
# ============================================================

class TestFigureChapterNumber:
    """T3：图号章序号必须归属到「实际存在的最高层级祖先」。

    回归前：`heading_gen.counters[0] or 1`，而 counters[0] 是 **L1 专属**计数器。
    目录树无 L1 时恒为 0 → 全文图表都落进 ch1 → 产出跨章节连续的
    「图 1-1、图 1-2」，图号失去章节归属（与图号虚跳同源）。
    """

    class _Gen:
        def __init__(self, counters):
            self.counters = counters

    def test_normal_tree_uses_l1_counter(self):
        """反例：正常含 L1 时行为必须与旧实现逐字一致。"""
        from app.routers.export import _figure_chapter_num
        assert _figure_chapter_num(self._Gen([3, 0, 0, 0, 0, 0, 0, 0])) == 3
        assert _figure_chapter_num(self._Gen([1, 2, 3, 0, 0, 0, 0, 0])) == 1

    def test_no_l1_falls_back_to_l2_counter(self):
        from app.routers.export import _figure_chapter_num
        assert _figure_chapter_num(self._Gen([0, 2, 0, 0, 0, 0, 0, 0])) == 2
        assert _figure_chapter_num(self._Gen([0, 3, 0, 0, 0, 0, 0, 0])) == 3

    def test_never_returns_zero(self):
        from app.routers.export import _figure_chapter_num
        for counters in ([0] * 8, [], [0, 0, 0, 0, 0, 0, 0, 0]):
            assert _figure_chapter_num(self._Gen(counters)) >= 1

    def test_deep_fallback_when_no_l1_or_l2(self):
        from app.routers.export import _figure_chapter_num
        assert _figure_chapter_num(self._Gen([0, 0, 4, 0, 0, 0, 0, 0])) == 4

    def test_robust_against_non_numeric(self):
        from app.routers.export import _figure_chapter_num
        assert _figure_chapter_num(self._Gen([None, "x", 5])) == 5
        assert _figure_chapter_num(self._Gen([None, "x", None])) == 1

    def test_three_call_sites_replaced(self):
        """反例：不得残留 `counters[0] or 1` 的旧写法。"""
        src = inspect.getsource(__import__("app.routers.export", fromlist=["x"]))
        assert "heading_gen.counters[0] if (" not in src
        assert src.count("_figure_chapter_num(heading_gen)") >= 3

    def test_end_to_end_two_chapters_render(self, tmp_path):
        sections = [
            {"id": "a", "parent_id": "", "sort_order": 0, "level": 1,
             "title": "第一部分", "content": "## 甲\n正文甲。\n"},
            {"id": "b", "parent_id": "", "sort_order": 1, "level": 1,
             "title": "第二部分", "content": "## 乙\n正文乙。\n"},
        ]
        path = _appendix_docx(tmp_path, [], sections, name="two_chapters.docx")
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        missing = [k for k in ("第一部分", "第二部分") if k not in xml]
        assert missing == [], f"章节渲染缺失：{missing}"


# ============================================================
# T11 目录编号 L1~L7 必须严格符合既定标准（防漂移护栏）
# ============================================================

class TestNumberingSpecConformance:
    """需求原文：L1 中文数字、L2 阿拉伯数字、L3 至 L4 带点、L5 带顿号、
    L6 带右括号、L7 带顿号。

    ⚠️ 本组是**防漂移护栏**，不是修复：2026-09-27 复核确认现有实现
    （services/ai/heading_v2._format_number + HEADING_STYLE_CONFIG 的
    punctuation）已经逐层符合该标准，因此**未做任何改动**。

    特别澄清一个容易改错的地方：L6 的标准是「带**右**括号」，
    正确形态是 `1）`（数字 + 全角右括号），**不是** `（1）`。
    若有人看到 `1）` 就"补左括号"，反而会偏离验收标准。
    """

    #: (层级, 编号文本, 与之相邻的分隔符) —— 分隔符来自 HEADING_STYLE_CONFIG
    _SPEC = [
        (1, "第一章", " "),   # L1 中文数字
        (2, "1", " "),        # L2 阿拉伯数字
        (3, "1.1", " "),      # L3 带点
        (4, "1.1.1", " "),    # L4 带点
        (5, "1.1.1.1", "、"),  # L5 带顿号
        (6, "1）", "、"),      # L6 带右括号（**不带左括号**）
        (7, "a", "、"),        # L7 带顿号
    ]

    def test_every_level_matches_spec(self):
        from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
        from app.services.ai.heading_templates import HEADING_STYLE_CONFIG
        got = {}
        for level, _expected, _p in self._SPEC:
            gen = HeadingNumberingGeneratorV2()
            got[level] = gen.update_counter(level, f"p{level}")
        wrong = {lv: (got[lv], exp) for lv, exp, _p in self._SPEC
                 if got[lv] != exp}
        assert wrong == {}, f"编号偏离标准（实际, 期望）：{wrong}"

    def test_punctuation_per_level(self):
        from app.services.ai.heading_templates import HEADING_STYLE_CONFIG
        wrong = {lv: (HEADING_STYLE_CONFIG[lv]["punctuation"], p)
                 for lv, _n, p in self._SPEC
                 if HEADING_STYLE_CONFIG[lv]["punctuation"] != p}
        assert wrong == {}, f"分隔符偏离标准（实际, 期望）：{wrong}"

    def test_l6_has_no_left_parenthesis(self):
        """反例：`（1）` 形态偏离「L6 带右括号」标准，必须被拦下。"""
        from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
        gen = HeadingNumberingGeneratorV2()
        num = gen.update_counter(6, "p6")
        assert "（" not in num, "L6 不得带左括号"
        assert num.endswith("）"), "L6 必须以全角右括号收尾"

    def test_templates_table_declares_the_same_spec(self):
        """DEFAULT_NUMBERING_TEMPLATES 模板表声明的形态与标准一致。

        ⚠️ 口径澄清（不要误写成"模板表输出 == 运行时输出"的不变量）：
        - `format_heading_by_id(id, level)` 是**路径驱动**的：它从传入的具体
          id 取末 N 段（`{last2}` → "6.7"）；
        - `heading_v2.update_counter()` 是**流式计数器**驱动的：它给"遇到的
          第一个节点"编号（第一章 / 1 / 1.1）。
        两者输入语义不同，直接比对是伪不变量。本用例只锁定**形态**：
        模板表里每层的占位符家族与 HEADING_STYLE_CONFIG 的分隔符必须与
        标准一致，从而即使有人改表也不会偏离验收标准。
        """
        from app.services.ai.heading_templates import (
            DEFAULT_NUMBERING_TEMPLATES, HEADING_STYLE_CONFIG)
        expected_placeholders = {
            1: "第{zh}章", 2: "{num}", 3: "{last2}", 4: "{last3}",
            5: "{last4}", 6: "{num}）", 7: "{alpha}",
        }
        wrong_tpl = {lv: (DEFAULT_NUMBERING_TEMPLATES[lv]["numbering_template"],
                          exp)
                     for lv, exp in expected_placeholders.items()
                     if DEFAULT_NUMBERING_TEMPLATES[lv]["numbering_template"]
                     != exp}
        assert wrong_tpl == {}, f"模板表形态偏离标准（实际, 期望）：{wrong_tpl}"
        wrong_punct = {lv: (HEADING_STYLE_CONFIG[lv]["punctuation"], p)
                       for lv, _n, p in self._SPEC
                       if HEADING_STYLE_CONFIG[lv]["punctuation"] != p}
        assert wrong_punct == {}, f"分隔符偏离标准：{wrong_punct}"

    def test_heading_spec_prompt_declares_l6_l7(self):
        """build_heading_spec_prompt 必须把 L6/L7 编号形态写清楚。

        该函数目前无生产调用方（编号靠事后 renumber 强制保证），但一旦将来
        被接进目录生成的 System Prompt，它的文本就成了 AI 的唯一依据。
        历史缺陷：规范文本只写「L6~L8 编号格式不限」，而实现侧实际产出
        `1）` / `a、` —— AI 无从得知，应当写明。
        （注：文本用「一级标题…六级标题」中文标签，不写 "L1"…字面量。）
        """
        from app.services.ai.heading_templates import build_heading_spec_prompt
        text = build_heading_spec_prompt()
        for label in ("一级标题", "二级标题", "三级标题", "四级标题",
                      "五级标题", "六级标题", "七级标题"):
            assert label in text, f"规范文本缺少「{label}」"
        # L6 必须是「数字 + 全角右括号」形态，且不得出现左括号变体
        assert "1）" in text, "L6 规范应体现「数字 + 右括号」"
        assert "（1）" not in text, "L6 规范不得写成「（1）」"
        # L7 顿号形态
        assert "a、" in text, "L7 规范应体现「字母 + 顿号」"
        # 禁止倾斜
        assert "禁止使用倾斜" in text, "规范必须声明标题不得倾斜"

    def test_display_conversion_roundtrip(self):
        """存储态编号 → 展示态编号（目录生成/正文/导出三模块的公共口径）。"""
        from app.services.numbering import (
            stored_id_to_display, stored_id_to_prefix)
        cases = [("3", "第三章", "3"), ("3.2", "2", "2"),
                 ("3.2.4", "2.4", "2.4"), ("3.2.4.5", "2.4.5", "2.4.5")]
        wrong = {sid: (stored_id_to_display(sid), stored_id_to_prefix(sid))
                 for sid, disp, pref in cases
                 if stored_id_to_display(sid) != disp
                 or stored_id_to_prefix(sid) != pref}
        assert wrong == {}, f"存储态→展示态转换漂移：{wrong}"

    def test_stored_outline_id_is_the_strict_gate(self):
        """非法存储编号的**唯一严格闸门**在 stored_outline_id（先取后展示）。

        `stored_id_to_display` 本身是"尽力而为"的：它按 "." 切分并只保留
        数字段，因此 "3.a" 会被降级成 "3" → "第三章"。这在历史脏数据场景下
        是合理的降级（不至于整章丢编号），但**必须**由上游的 stored_outline_id
        先把非法 id 判死 —— 本用例锁定这个先后顺序不被调换。
        """
        import json
        from app.services.numbering import stored_outline_id
        assert stored_outline_id(
            {"outline_json": json.dumps({"id": "1.2"})}) == "1.2"
        for bad in ("3.a", "abc", "uuid-1234", ""):
            sec = {"outline_json": json.dumps({"id": bad})}
            assert stored_outline_id(sec) == "", f"{bad!r} 应被闸门判死"
        # 非法 outline_json / 缺失同样判死
        assert stored_outline_id({}) == ""
        assert stored_outline_id({"outline_json": "{not json"}) == ""


# ============================================================
# T7 缺图文档不得写入缓存
# ============================================================

class TestTableMarkupStripNotEatingBodyText:
    """✅ BUG 修复（2026-09-27）：正文比较式被当 HTML 标签整段删除。

    背景：``_HTML_TABLE_TAG_RE`` 的属性段原为 ``[^>]*``，允许「标签名」与「>」之间
    夹任意文本；且 ``strip_table_markup`` 的触发门槛只是「出现 ``<``」。两者叠加导致
    工程正文里合法的比较式被静默删除：

        「当 T<P 且 Q>R 时设计」→「当 TR 时设计」

    用户与导出日志均无任何提示，违反 AGENTS.md §3.1.6「数据真实性红线」。
    修复：① 属性段按真实 HTML 属性语法收紧（属性名必须 ASCII 标识符）；② 门控为
    「确实检出剪贴板占位标记或真实表格结构」才降级。
    """

    # 反例回归：正文比较式 / 中文夹带尖括号必须逐字节保留
    @pytest.mark.parametrize("text", [
        "当 T<P 且 Q>R 时设计",
        "当 a <p 且 b > c 时设计",
        "a<b 且 c>d",
        "f(x) < 3",
        "支座反力 R<V 且剪力 Q<0",
    ])
    def test_body_comparison_preserved(self, text):
        assert clean_text(text) == text
        assert strip_table_markup(text) == text

    def test_clipboard_soup_still_degraded(self):
        """正例回归：剪贴板标签汤仍须降级为可读纯文本，不能被误伤。"""
        assert "甲" in clean_text("<table><fcel><fcel><nl>甲</table>")
        assert clean_text("<td>甲</td><td>乙</td>").strip() == "甲 乙"

    def test_real_html_tag_with_attributes_still_stripped(self):
        """正例回归：真实标签（含属性）仍须被清理，门控不误伤。"""
        assert strip_table_markup('<p class="x">正文</p>') == "正文"
        assert strip_table_markup("<span>甲</span>") == "甲"

    def test_table_structure_gates_the_downgrade(self):
        """含真实表格结构时，降级照常生效。"""
        out = strip_table_markup("<table><tr><td>甲</td><td>乙</td></tr></table>")
        assert "甲" in out and "乙" in out
        assert "<td>" not in out and "<table>" not in out

    def test_mixed_body_and_table_keeps_body(self):
        """同一段落里既有比较式又有表格结构时，正文部分不得丢失。"""
        src = "验算 T<P 且 Q>R：<td>甲</td>"
        out = strip_table_markup(src)
        assert "T<P 且 Q>R" in out
        assert "甲" in out



    """T7：`_ai_converted` 返回值曾被丢弃 → 缺图成稿被写进 export_cache，
    AI 生图服务恢复后因内容指纹不变而**永久**命中这份残缺文档
    （与代码注释承诺的「服务恢复后可重试」直接矛盾）。
    """

    def test_prep_declares_ai_image_pending(self):
        from app.routers.export import _prepare_export
        assert "ai_image_pending" in inspect.getsource(_prepare_export)

    def test_export_docx_guard_reads_prep_field(self):
        from app.routers.export import export_docx
        src = inspect.getsource(export_docx)
        assert 'prep.get("ai_image_pending", 0)' in src
        assert '"X-Cache-Status": "degraded"' in src

    def test_pending_count_gated_by_autogen_switch(self):
        """反例：用户显式关闭 ai_image_auto_generate 时占位块残留是既定行为，
        此时判 degraded 会让该方案导出缓存永久失效 —— 那是回归。"""
        from app.routers.export import _prepare_export
        src = inspect.getsource(_prepare_export)
        assert 'config.get("ai_image_auto_generate", True)' in src

    def test_converted_count_is_no_longer_discarded(self):
        """反例：返回值必须真正被使用（不再是悬空赋值）。"""
        from app.routers.export import _prepare_export
        src = inspect.getsource(_prepare_export)
        assert "_ai_converted" in src
        # 出现次数 >= 2：赋值 + 使用（回归前只有 1 次赋值）
        assert src.count("_ai_converted") >= 2

