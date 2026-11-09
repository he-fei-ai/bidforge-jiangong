"""R195 导出模块收口护栏（2026-11-09）。

覆盖本轮后端修复面（行为实证 + 源码棘轮锁）：
  L1  _inflight_final 注册前初始化（UnboundLocalError 回归收口，两条链）
  L2  章级丢章 block_render_failed → 坏缓存守卫 + X-Export-Degraded: section-dropped
  L3  dossier 链闸门补齐 + 产物名纳入 GC 扫描前缀
  L4  dossier 服务层 XML 非法字符净化（行为实证）
  L5  零宽字符：交付清洗 + 失效引用检出容忍
  L6  表号剥离补「表格 X-Y」/全角数字形态
  L7  预检 fail-open 可见化（X-Export-Gate: unchecked）
"""

from __future__ import annotations  # noqa: I001 — 下方 app 导入为循环依赖保留顺序，块级抑制 I001

import io
import os
import re

import pytest

# ⚠️ 导入顺序敏感（勿按字母序重排）：export 与 _export_docx_parts 互为 facade
#    循环引用，必须先 import export 触发其链路加载，再引 parts/iscan 才安全。
from app.routers import export as E  # noqa: I001,E402
from app.routers import _export_docx_parts as parts  # noqa: I001,E402
from app.routers import _export_issue_scan as iscan  # noqa: I001,E402
from app.services import docx_math as dm  # noqa: I001,E402
from app.services import dossier as doss  # noqa: I001,E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT_SRC = io.open(os.path.join(REPO_ROOT, "app", "routers", "export.py"), encoding="utf-8").read()
DOSSIER_ROUTE_SRC = io.open(
    os.path.join(REPO_ROOT, "app", "routers", "export_dossier.py"), encoding="utf-8"
).read()


# ===========================================================================
# L1 · _inflight_final 注册前初始化（两条链）
# ===========================================================================
class TestInflightFinalInit:
    def test_both_chains_initialize_before_try(self):
        """DOCX/PDF 两条链的 finally 注销兜底依赖 _inflight_final；
        注册点之前的任何异常路径（互斥 409 / 闸门 409 / 取消 499 / prepare 失败）
        都会走到注销行 —— 未初始化即 UnboundLocalError 掩盖原始异常
        （复跑 16 failed 中 12 例的根因）。"""
        pattern = re.compile(r"_lk = None[\s\S]{0,400}?_inflight_final = None[\s\S]{0,200}?\n    try:")
        assert len(pattern.findall(EXPORT_SRC)) == 2, "两条导出链都必须在 try 前初始化 _inflight_final"

    def test_finally_unregister_present(self):
        """两条链 finally 注销兜底必须在（R195 修复断连残留的主体）。"""
        assert EXPORT_SRC.count("_export_inflight_unregister(_inflight_final)") >= 3


# ===========================================================================
# L2 · 章级丢章 → 坏缓存守卫 + section-dropped 头
# ===========================================================================
class TestSectionDroppedGuard:
    def test_helper_counts_block_render_failed(self):
        assert E._export_section_dropped({"block_render_failed": 2}) == 2
        assert E._export_section_dropped({}) == 0
        assert E._export_section_dropped(None) == 0
        assert E._export_section_dropped({"block_render_failed": "x"}) == 0  # 防御异常统计值

    def test_both_chains_wire_criteria_and_header(self):
        """DOCX 与 PDF 降级判据都接 _section_dropped，且都发 section-dropped 头。"""
        assert EXPORT_SRC.count("_export_section_dropped(fix_stats) > 0") == 2
        assert EXPORT_SRC.count('"section-dropped"') == 2


# ===========================================================================
# L3 · dossier 链闸门补齐 + GC 前缀
# ===========================================================================
class TestDossierRouteGates:
    def test_gate_and_numbering_guard_wired(self):
        assert "_export_high_issue_gate" in DOSSIER_ROUTE_SRC, "底稿链必须接 DLV 阻断闸门"
        assert "_guard_numbering_consistency" in DOSSIER_ROUTE_SRC, "底稿链必须接编号一致性守卫"

    def test_blocked_response_returned_not_raised(self):
        """_export_blocked_response 返回 JSONResponse（不得 raise）。"""
        assert "return export_router._export_blocked_response(_blockers)" in DOSSIER_ROUTE_SRC

    def test_product_name_in_gc_prefix_scope(self):
        """产物名以 {scheme_id}_ 开头才落 _gc_orphan_exports 扫描范围
        （BackgroundTask 断连/崩溃不执行 → 旧名 dossier_{sid}_* 永久孤儿）。"""
        assert 'EXPORTS_DIR / f"{scheme_id}_dossier_' in DOSSIER_ROUTE_SRC

    async def test_high_issues_block_dossier_route(self, db_conn, monkeypatch):
        """行为实证：底稿链检出 high 级问题（无审核记录 → DLV-09）时返回
        与 /export/docx 同构的 409 阻断体（旧实现完全旁路闸门）。"""
        import uuid

        from app.config import settings as app_settings
        from fastapi.responses import JSONResponse

        monkeypatch.setattr(app_settings, "dossier_enabled", True)
        pid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        await db_conn.execute("INSERT INTO projects (id, name) VALUES (?, ?)", (pid, "闸门方案"))
        await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?, ?, ?)", (sid, pid, "闸门方案"))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content, sort_order) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), sid, pid, "工程概况", "正文", 0),
        )
        await db_conn.commit()
        resp = await __import__("app.routers.export_dossier", fromlist=["export_dossier"]).export_dossier(
            sid, db=db_conn
        )
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 409
        body = __import__("json").loads(resp.body)
        assert body["error"] == "export_blocked_high_issues"
        assert any(b["rule_id"] == "DLV-09" for b in body["high_issues"])


# ===========================================================================
# L4 · dossier 服务层净化（行为实证）
# ===========================================================================
class TestDossierSafeText:
    def test_strips_xml_illegal_and_zero_width(self):
        assert doss._safe_text("a\x00b\x0bc\x1fd") == "abcd"
        assert doss._safe_text("图\u200b9-9\ufeff") == "图9-9"
        assert doss._safe_text(None) == ""
        assert doss._safe_text(3) == "3"
        assert doss._safe_text("\ufffe\uffff") == ""

    def test_append_to_docx_survives_illegal_chars(self, tmp_path):
        """DB 文本含 XML 非法字符时底稿导出不得 500（旧行为：lxml 序列化抛 ValueError）。"""
        from docx import Document

        base = tmp_path / "base.docx"
        Document().save(str(base))
        data = {
            "scheme_id": "s1",
            "scheme_name": "测试方案\x00",
            "defects": {
                "items": [
                    {
                        "rule_id": "R\x0b1",
                        "dimension": "维度",
                        "status": "suggested",
                        "fix_policy": "protected",
                        "title": "标题\x1f",
                        "before_text": "修复前\x00文本",
                        "after_text": "修复后",
                        "suggestion": "建议",
                    }
                ],
                "decisions": [],
                "error": "db 错误\x00信息",
            },
            "facts": {"items": [], "error": ""},
            "clauses": {"available": False, "items": [], "statement": doss.CLAUSE_LIBRARY_DECLARATION},
            "standards": {"available": False, "items": [], "statement": doss.STANDARD_VERIFICATION_DECLARATION},
            "pending_defects": [],
        }
        doss.append_to_docx(base, data)  # 不得抛
        # 读回 XML 层校验（zip 压缩流可含任意字节，不能对整包做字节断言）
        import zipfile

        with zipfile.ZipFile(str(base)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8")
        assert "\x00" not in xml
        assert "\x0b" not in xml and "\x1f" not in xml
        # 渲染层读回也不得抛（python-docx/lxml 二次校验）
        Document(str(base))


# ===========================================================================
# L5 · 零宽字符
# ===========================================================================
class TestZeroWidthHandling:
    def test_delivery_cleaning_removes_zero_width(self):
        assert parts._xml_safe_text("a\u200bb\u200cc\u200dd\u2060e\ufefff") == "abcdef"
        # 既有行为不回归：XML 非法控制字符仍剥除、合法空白保留
        assert parts._xml_safe_text("a\x00b\tc\nd\re") == "ab\tc\nd\re"

    def test_stale_ref_detection_tolerates_zero_width(self):
        assert iscan._STALE_FIG_REF_RE.search("图\u200b9-9").group(1) == "9-9"
        assert iscan._STALE_TBL_REF_RE.search("表\u200b3-1").group(1) == "3-1"
        # 既有形态不回归
        assert iscan._STALE_FIG_REF_RE.search("图 9-9").group(1) == "9-9"
        assert iscan._STALE_FIG_REF_RE.search("图9-9").group(1) == "9-9"
        assert iscan._STALE_FIG_REF_RE.search("正常文字") is None


# ===========================================================================
# L6 · 表号剥离漏网形态
# ===========================================================================
class TestLeadingTableNumberStrip:
    def test_biaoge_form_stripped(self):
        assert parts._strip_leading_table_number("表格 6-1 木工班组劳动力配置计划") == "木工班组劳动力配置计划"

    def test_fullwidth_digits_stripped(self):
        assert parts._strip_leading_table_number("表６-１ 木工班组") == "木工班组"

    def test_zero_width_tolerated(self):
        assert parts._strip_leading_table_number("表\u200b6-1 木工班组") == "木工班组"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("表 6-1 木工班组", "木工班组"),  # 既有形态
            ("表：劳动力计划", "劳动力计划"),  # 既有形态
            ("表6-1 劳动力", "劳动力"),  # 无空格
            ("表格内容说明", "表格内容说明"),  # 「表格」后无编号 → 不剥
            ("表示 3-2 会误剥", "表示 3-2 会误剥"),  # 非「表格」前缀 → 不剥
        ],
    )
    def test_regression_matrix(self, raw, expected):
        assert parts._strip_leading_table_number(raw) == expected


# ===========================================================================
# L7 · 预检 fail-open 可见化
# ===========================================================================
class TestGateUncheckedHeader:
    def test_unchecked_emits_gate_header(self):
        assert E._export_override_headers({"checked": False, "override": False}) == {
            "X-Export-Gate": "unchecked"
        }

    def test_override_priority_unchanged(self):
        assert E._export_override_headers({"override": True}) == {E._EXPORT_OVERRIDE_HEADER: "high-issues"}

    def test_normal_check_no_header(self):
        assert E._export_override_headers({"checked": True, "override": False}) == {}
        assert E._export_override_headers({}) == {}


# ===========================================================================
# L8 · 零宽字符在主正文路径（docx_math.clean_text）真正生效
#     R195 批次②只补了 _xml_safe_text（底稿/交付子路径）；主正文经
#     _add_runs_with_inline_format → clean_text，未覆盖会原样印进成稿。
# ===========================================================================
class TestZeroWidthCleanText:
    def test_zero_width_stripped(self):
        for zw in ("\u200b", "\u200c", "\u2060", "\ufeff"):
            text = f"正文{zw}夹带{zw}零宽"
            assert zw not in dm.clean_text(text), f"{zw!r} 应被 clean_text 剥除"

    def test_zwj_preserved(self):
        # U+200D（ZWJ）刻意保留，避免误拆 emoji / 复杂文字连写
        assert "\u200d" in dm.clean_text("emoji\u200d连写")

    def test_control_chars_still_stripped(self):
        assert "\x00" not in dm.clean_text("含\x00控制符")
        assert "\x1f" not in dm.clean_text("含\x1f控制符")

    def test_normal_text_unchanged(self):
        assert dm.clean_text("正常中文段落，无零宽字符。") == "正常中文段落，无零宽字符。"


# ===========================================================================
# L9 · 超长段落 fail-soft 截断（与表格尺寸上限同口径）
# ===========================================================================
class TestParagraphClamp:
    def test_normal_paragraph_unchanged(self):
        text = "a" * 5000
        assert parts._clamp_paragraph_text(text) == text

    def test_overlong_truncated(self):
        text = "x" * (parts._PARAGRAPH_MAX_CHARS + 100)
        out = parts._clamp_paragraph_text(text)
        assert len(out) == parts._PARAGRAPH_MAX_CHARS
        assert out == text[: parts._PARAGRAPH_MAX_CHARS]

    def test_counter_increments(self):
        parts._FIX_STATS_LOCAL.stats = {k: 0 for k in parts._FIX_STATS_KEYS}
        parts._clamp_paragraph_text("y" * (parts._PARAGRAPH_MAX_CHARS + 1))
        assert parts._fix_stats()["paragraph_truncated"] == 1
        # 未超长不计数
        parts._clamp_paragraph_text("短段落")
        assert parts._fix_stats()["paragraph_truncated"] == 1


# ===========================================================================
# L10 · 专用 COM 执行器（P2-1 防御性加固）
#      PDF 转换经隔离的单线程执行器，不占用默认 asyncio 线程池。
# ===========================================================================
class TestPdfComExecutor:
    def test_executor_is_dedicated_single_worker(self):
        import concurrent.futures as _cf

        assert isinstance(E._PDF_COM_EXECUTOR, _cf.ThreadPoolExecutor)
        assert E._PDF_COM_EXECUTOR._max_workers == 1

    def test_dispatch_uses_dedicated_executor(self):
        # 源码棘轮：PDF 转换派发必须经 _PDF_COM_EXECUTOR（而非 asyncio.to_thread）
        assert "run_in_executor(_PDF_COM_EXECUTOR, _convert_docx_to_pdf," in EXPORT_SRC

    def test_max_workers_constant(self):
        # 串行锁已保证至多 1 个并发转换，max_workers=1 即可（隔离而非扩容）
        assert "_PDF_COM_EXECUTOR = _cf.ThreadPoolExecutor(max_workers=1," in EXPORT_SRC
