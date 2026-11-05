"""解析提取模块遗留收口 R176 配套护栏（2026-11-03）。

覆盖本轮遗留分诊结果（与 R53/R72/R73/R79/R171/R175 既有护栏不重复）：

  T1 棘轮：孤路由 /upload-outline/{id}/save-as-outline 的前端调用已于 2026-09-20
           移除（api/index.ts:345 注释），后端保留路由仅供直连/测试。本组锁
           「前端不得回流调用」，防止孤路由双轨复活；
  G6-min 源码锁：parse-all 批量解析的三条进度 INFO（批始/逐份/批末）必须在位
           ——纯日志接线，摘除即运维失去批量解析可观测性；
  G9 闭合棘轮：OCR 页数截断告警的「如何调大」引导文案必须在位（R175 审计 G9
           称缺引导，复核证伪：文案先于审计存在，本棘轮防其日后丢失）；
  自证组：棘轮判据自身非空（扫描集/函数定位不为空），防假绿。

测试风格与既有模块一致：纯源码/文件锁 + 直接调用，不引入 TestClient。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ===========================================================================
# A 组 · T1 棘轮：前端不得回流调用 save-as-outline 孤路由
# ===========================================================================


class TestSaveAsOutlineFrontendRatchet:
    """前端调用已移除（2026-09-20）；后端路由保留仅为直连/测试兼容。

    若产品决策重新启用该入口，应有意识地更新本棘轮，而不是静默回流
    ——回流即恢复「与 outline_library.applyAndSave 并存的双轨 sections 重建」。
    """

    def test_frontend_has_no_active_save_as_outline_call(self):
        src_dir = ROOT / "frontend" / "src"
        assert src_dir.is_dir(), f"前端源码目录不存在：{src_dir}"
        scanned = 0
        offenders: list[str] = []
        for p in src_dir.rglob("*"):
            if p.suffix not in (".ts", ".tsx"):
                continue
            scanned += 1
            for i, line in enumerate(_read(p).splitlines(), 1):
                # 剥离行注释后再判：api/index.ts:345 的移除说明注释允许保留
                code = line.split("//", 1)[0]
                if "save-as-outline" in code or "saveAsOutline" in code:
                    offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:100]}")
        assert scanned > 50, f"前端扫描集异常偏少（scanned={scanned}），判据失效"
        assert offenders == [], "前端不得回流调用 save-as-outline 孤路由：\n" + "\n".join(offenders)

    def test_backend_orphan_route_still_registered(self):
        """后端孤路由保留现状锁：若下线（删除路由），应同步删除其 57 例测试
        并更新 R175 审计报告 T1 登记，属独立决策项。"""
        src = _read(BACKEND / "app" / "routers" / "upload_outline.py")
        assert '@router.post("/{upload_id}/save-as-outline")' in src


# ===========================================================================
# B 组 · G6-min 源码锁：parse-all 三条进度日志必须在位
# ===========================================================================


def _parse_all_source() -> str:
    src = _read(BACKEND / "app" / "routers" / "global_facts_docs.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "parse_all_documents":
            return ast.get_source_segment(src, node) or ""
    pytest.fail("未找到 parse_all_documents 函数（函数被重命名/移除？）——判据需重锚")


class TestParseAllProgressLogging:
    """G6-min（R176）：批量解析进度可观测性（纯日志，零契约变更）。

    变异即红：摘除任一条 INFO、或把 enumerate 改回裸 for（_seq 失效），
    都会让批量解析在日志里重新变成黑盒。
    """

    def test_three_progress_info_lines_present(self):
        seg = _parse_all_source()
        tree = ast.parse(seg)
        info_calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "info"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "logger"
        ]
        # 批始 1 + 逐份 1 + 批末 1 = 3
        assert len(info_calls) >= 3, f"parse_all_documents 的 logger.info 进度日志不足（{len(info_calls)} < 3）"

    def test_loop_enumerates_for_sequence(self):
        seg = _parse_all_source()
        assert "enumerate(pending, 1)" in seg, "批量解析循环丢失 enumerate 序号（逐份进度 i/N 判据失效）"

    def test_log_messages_self_certified(self):
        """三条日志文案各就各位（防「计数够但语义漂移」）。"""
        seg = _parse_all_source()
        assert "批量解析开始" in seg
        assert "批量解析进度" in seg
        assert "批量解析完成" in seg


# ===========================================================================
# C 组 · G9 闭合棘轮：OCR 截断告警的调大引导不得丢失
# ===========================================================================


class TestOcrPageLimitGuidanceRatchet:
    """R175 审计 G9 称「缺调大引导」，复核证伪：引导文案先于审计存在
    （file_parser 截断告警 + config 可调项）。本组防引导日后被改丢。"""

    def test_ocr_truncation_warning_carries_guidance(self):
        src = _read(BACKEND / "app" / "services" / "file_parser.py")
        assert "可调大 OCR_PDF_MAX_PAGES" in src, "OCR 页数截断告警丢失「如何调大」引导文案"

    def test_ocr_page_limit_configurable(self):
        cfg = _read(BACKEND / "app" / "config.py")
        assert "ocr_pdf_max_pages" in cfg, "OCR 页上限配置项丢失"
        assert "ocr_pdf_dpi" in cfg, "OCR DPI 配置项丢失"


# ===========================================================================
# D 组 · 自证：棘轮判据自身非空（防假绿）
# ===========================================================================


class TestRatchetSelfCertification:
    """把判据自身归零后必须红（此处在测试内自证扫描面非空）。"""

    def test_ratchet_targets_exist(self):
        assert (ROOT / "frontend" / "src" / "api" / "index.ts").exists()
        assert (BACKEND / "app" / "routers" / "global_facts_docs.py").exists()
        assert (BACKEND / "app" / "services" / "file_parser.py").exists()

    def test_frontend_comment_anchor_still_documents_removal(self):
        """移除说明注释仍在（A 组判据依赖「注释不算调用」的剥离逻辑）。"""
        api_ts = _read(ROOT / "frontend" / "src" / "api" / "index.ts")
        removal_comments = [ln for ln in api_ts.splitlines() if ln.strip().startswith("//") and "save-as-outline" in ln]
        assert removal_comments, "移除说明注释丢失——请核对 A 组棘轮的注释剥离判据"
