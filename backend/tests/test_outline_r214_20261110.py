"""R214（2026-11-10）· 目录生成模块遗留项收口 —— 新增/重锚护栏。

承接 R213 §10 的 6 项遗留 + R209/R210 仍登记的 3 项，逐条重新取证后定夺：
落地 5 项（提示词编号统一 / 版本路径统一 / 行尾归一 / lint / 超时）、
证伪加固 3 项（熔断作用域 / trace_id / export_audit）、登记 2 项。

所有断言均基于活体代码或可复现的纯函数行为，不采信报告结论。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND_APP = REPO / "backend" / "app"
FRONTEND_SRC = REPO / "frontend" / "src"

#: R214 行尾归一**精确清单**（35 个存量 CRLF + 2 个本轮新增文件）。
#: 刻意不用目录级 glob：`schemeWorkbench/` 下混有 bidAnalysis / 事实等其它
#: 模块文件，实测有外部编辑器把 `useBidAnalysis.tsx` 反复写回 CRLF ⇒
#: 按模块名筛后的清单才是本轮可承诺、可回归的集合（承 R213「按模块名筛」）。
_R214_NORMALIZED_FILES = (
    "components/OutlineLibraryEditModal.tsx",
    "pages/OutlineLibraryPage.tsx",
    "pages/schemeWorkbench/ElapsedTimer.tsx",
    "pages/schemeWorkbench/exportShared.ts",
    "pages/schemeWorkbench/FactsAdjustPanel.tsx",
    "pages/schemeWorkbench/FactsCategoryPanel.tsx",
    "pages/schemeWorkbench/FactsChapterCoveragePanel.tsx",
    "pages/schemeWorkbench/FactsDiagnosticsPanel.tsx",
    "pages/schemeWorkbench/FactsExtractProgressCard.tsx",
    "pages/schemeWorkbench/FactsSegmentFailuresAlert.tsx",
    "pages/schemeWorkbench/GenerationProgressCard.tsx",
    "pages/schemeWorkbench/logTypes.ts",
    "pages/schemeWorkbench/NumberingConsistencyPanel.tsx",
    "pages/schemeWorkbench/OutlineGateList.tsx",
    "pages/schemeWorkbench/OutlineNodeBudgetPanel.tsx",
    "pages/schemeWorkbench/outlineNumbering.ts",
    "pages/schemeWorkbench/OutlineTreeActions.tsx",
    "pages/schemeWorkbench/outlineTreeModel.ts",
    "pages/schemeWorkbench/SchemeTreePanel.tsx",
    "pages/schemeWorkbench/SectionLogList.tsx",
    "pages/schemeWorkbench/statsFormat.ts",
    "pages/schemeWorkbench/TaskControlButtons.tsx",
    "tests/outlineGateListR174.test.tsx",
    "tests/OutlineLibraryEditModal.test.tsx",
    "tests/OutlineLibraryPage.test.tsx",
    "tests/outlineNumberingR87.test.ts",
    "tests/outlinePollRecovery.test.ts",
    "tests/outlineQuality.test.ts",
    "tests/outlineReviewBasisR74.test.ts",
    "tests/outlineStoppedCkptGuard.test.ts",
    "tests/outlineTab.test.tsx",
    "tests/outlineTaskReattach.test.ts",
    "tests/outlineTreeLogic.test.ts",
    "tests/uploadOutlineApi.test.ts",
    "utils/outlineConstants.ts",
    "pages/schemeWorkbench/outlineNotices.ts",  # R213 新增
    "tests/outlineR214.test.ts",  # R214 新增
)


def _src(rel: str) -> str:
    return (BACKEND_APP / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A · FIX-1 提示词编号统一（承 R210 L1-1 / R213 L-1）
# ---------------------------------------------------------------------------


class TestPromptNumberingUnified:
    @pytest.mark.parametrize(
        "stored,full_title,expect",
        [
            ("3", "第三章 工程概况", "3"),
            ("3.2", "2 进度计划", "2"),
            ("3.2.4", "2.4 施工准备", "2.4"),
        ],
    )
    def test_prefix_matches_export(self, stored, full_title, expect):
        """提示词子标题前缀 == 导出展示标题的数字前缀（三档逐字同结论）。"""
        from app.routers.export import _section_number_prefix
        from app.services.numbering import stored_id_to_prefix

        assert stored_id_to_prefix(stored) == expect
        assert _section_number_prefix(full_title) == expect

    def test_display_number_shape(self):
        from app.services.numbering import stored_id_to_display

        assert stored_id_to_display("3") == "第三章"
        assert stored_id_to_display("3.2") == "2"
        assert stored_id_to_display("3.2.4") == "2.4"

    def test_rule_uses_export_prefix_not_stored_id(self):
        from app.services.ai.heading_templates import build_subheading_rule
        from app.services.numbering import stored_id_to_prefix

        rule = build_subheading_rule(section_prefix=stored_id_to_prefix("3.2"))
        assert "2.1 标题" in rule
        assert "2.1.1 标题" in rule
        assert "3.2.1" not in rule

    def test_router_wires_both_values(self):
        # ✅ R223 重锚（锁实现 → 锁意图，强度不减）：R214 首版把「抢救回退」的
        #    `or _sec_stored` 实现形态逐字锁死；R223 NUM-1 判定该抢救形态对
        #    0 号段脏编号（display/prefix 判空）会把非法值注入提示词，与 R218
        #    C-3「非法统一注入缺失占位」契约矛盾 ⇒ 已移除，改为「display/prefix
        #    任一为空即按编号缺失口径」。意图不变且加强：展示态/前缀仍同源接线、
        #    5 处 {section_number} 仍复用同一变量、非法值不再流入提示词。
        src = _src("routers/sse_handlers.py")
        assert "_sec_no = stored_id_to_display(_sec_stored)" in src
        assert "_sec_prefix = stored_id_to_prefix(_sec_stored)" in src
        assert "or _sec_stored" not in src, "抢救回退必须退役（0 号段脏编号会被原样注入提示词）"
        assert "section_prefix=_sec_prefix" in src
        assert "section_prefix=_sec_no" not in src
        # 5 处 {section_number} 注入仍复用同一个展示态变量
        assert src.count("section_number=_sec_no") >= 4

    def test_cross_reference_no_longer_dangles(self):
        """反证：旧写法标题被纠偏、正文交叉引用却不变 ⇒ 悬空；新写法逐字幂等。"""
        from app.services.numbering import renumber_section_body_subheadings

        old = "## 3.2.1 总体安排\n详见 3.2.1 节的施工部署。"
        new = "## 2.1 总体安排\n详见 2.1 节的施工部署。"
        old_out, changes = renumber_section_body_subheadings(old, "3.2", 2)
        assert "## 2.1 总体安排" in old_out
        assert "详见 3.2.1 节" in old_out  # 交叉引用悬空（旧契约）
        assert changes
        new_out, changes2 = renumber_section_body_subheadings(new, "3.2", 2)
        assert new_out == new
        assert not changes2  # 模型按新规则写出的编号落库零改写


# ---------------------------------------------------------------------------
# B · FIX-2 目录库写路径统一出口
# ---------------------------------------------------------------------------


class TestLibraryWriteSingleExit:
    def test_four_paths_use_helper_and_old_impl_cannot_return(self):
        src = _src("routers/outline_library.py")

        # ⚠️ 按调用点的 ``context`` 锁定，而不是按函数边界：R190 把
        #    new_version 拆成「写锁薄壳 → _new_version_locked」，函数名与
        #    真实承载函数并不一一对应（inspect.getsource 还会因旧 pyc 错位）。
        for ctx in ("new_version", "restore_version", "from_scheme", "duplicate"):
            assert f'context="{ctx}"' in src, ctx
        assert src.count("_normalize_for_write(") >= 5  # 1 定义 + 4 调用
        assert "normalize_outline_json(" not in src  # 旧弱规范化不得回流
        assert "normalize_outline_for_library" in src  # 统一出口的唯一实现
        assert "def _normalize_outline(" not in src  # 无消费者的薄包装已删

    def test_restore_rejects_empty_history(self):
        src = _src("routers/outline_library.py")
        assert "历史版本目录内容为空或格式非法，无法回滚" in src
        assert "_restored_nodes" in src
        assert 'context="restore_version"' in src

    def test_helper_is_fail_closed_and_lenient_on_problems(self, caplog):
        from app.routers.outline_library import _normalize_for_write

        # 非法载荷 → 空目录（由调用方 400），不抛异常、不回吐原始结构
        assert _normalize_for_write({"foo": 1}, context="test") == "[]"
        # 脏历史版本：编号重排 + 只 WARNING 不 400（恢复动作不被旧数据卡死）
        dirty = [
            {
                "id": "7",
                "title": "A" * 250,
                "children": [{"id": "9.9", "title": "B"}],
            }
        ]
        out = _normalize_for_write(dirty, context="test-version", library_id="lib-1")
        assert json.loads(out)[0]["id"] == "1"
        assert any("目录库写路径" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# C · FIX-3 行尾归一棘轮（.gitattributes eol=lf）
# ---------------------------------------------------------------------------


class TestOutlineFrontendEolRatchet:
    def test_normalized_files_are_pure_lf(self):
        assert len(_R214_NORMALIZED_FILES) == 37, "归一清单被改动（棘轮）"
        bad = []
        for rel in _R214_NORMALIZED_FILES:
            p = FRONTEND_SRC / rel
            assert p.exists(), rel
            b = p.read_bytes()
            if b"\r\n" in b or b.startswith(b"\xef\xbb\xbf") or b"\xef\xbf\xbd" in b:
                bad.append(rel)
        assert not bad, f"以下文件行尾/字节不合规: {bad[:5]}"

    def test_new_files_must_be_registered(self):
        """棘轮：新增的 R213/R214 目录模块文件必须在归一清单内。"""
        assert "pages/schemeWorkbench/outlineNotices.ts" in _R214_NORMALIZED_FILES
        assert "tests/outlineR214.test.ts" in _R214_NORMALIZED_FILES

    def test_vitest_config_timeout_is_file_scoped(self):
        """FIX-5：超时放宽只落在重测试文件，全局 config 不动。"""
        page_test = (FRONTEND_SRC / "tests" / "schemeWorkbench.test.tsx").read_text(encoding="utf-8")
        cfg = (REPO / "frontend" / "vitest.config.ts").read_text(encoding="utf-8")
        assert "vi.setConfig({ testTimeout: 120000, hookTimeout: 120000 })" in page_test
        assert "testTimeout: 60000" in cfg
        assert "hookTimeout: 30000" in cfg


# ---------------------------------------------------------------------------
# D · FIX-4 lint（既有 I001）
# ---------------------------------------------------------------------------


class TestSectionsImportOrder:
    def test_ruff_i001_fixed_order_is_locked(self):
        src = _src("routers/sections.py")
        i_norm = src.index("normalize_section_content_subheadings")
        i_list = src.index("list_numbering_versions as _list_numbering_versions")
        assert i_norm < i_list


# ---------------------------------------------------------------------------
# E · L-5 熔断作用域（证伪「缺陷」+ 加固）
# ---------------------------------------------------------------------------


class TestQuotaBreakerScopeRatchet:
    def test_marker_is_bounded_and_task_scoped(self):
        from app.routers import _sse_outline_review as rv

        rv._QUOTA_BLOCKED_TASKS.clear()
        try:
            for i in range(200):
                rv._mark_quota_blocked(f"t{i}")
            assert len(rv._QUOTA_BLOCKED_TASKS) <= 128  # 有界防泄漏
            rv._QUOTA_BLOCKED_TASKS.clear()
            rv._mark_quota_blocked("a")
            assert rv._is_quota_blocked("a")
            assert not rv._is_quota_blocked("b")  # 任务级隔离
            rv._clear_quota_blocked("a")
            rv._clear_quota_blocked("a")  # 幂等
            assert not rv._QUOTA_BLOCKED_TASKS
        finally:
            rv._QUOTA_BLOCKED_TASKS.clear()

    def test_task_registry_is_process_local(self):
        """熔断标记与任务注册表同为进程内内存态 ⇒ 单进程架构下无缺陷。"""
        src = _src("services/ai/task_registry.py")
        assert "_tasks" in src
        assert "dict" in src
        handlers = _src("routers/sse_handlers.py")
        assert handlers.count("_clear_quota_blocked(task_id)") == 2  # 入口 + 终态


# ---------------------------------------------------------------------------
# F · L-6 历史登记项证伪加固
# ---------------------------------------------------------------------------


class TestHistoricalRegistrationsReaffirmed:
    def test_outline_write_logs_carry_scheme_anchor(self):
        """trace_id 价值不足：各写路径已有 scheme_id/section_id 锚点。"""
        for rel in (
            "routers/sections.py",
            "routers/outline_library.py",
            "routers/sse_handlers.py",
        ):
            src = _src(rel)
            assert "scheme=%s" in src or "scheme_id" in src, rel

    def test_export_audit_module_stays_deleted(self):
        """R198 已物理删除，能力迁入 _export_issue_scan；不得复活。"""
        assert not (BACKEND_APP / "routers" / "export_audit.py").exists()
        assert (BACKEND_APP / "routers" / "_export_issue_scan.py").exists()


# ---------------------------------------------------------------------------
# G · 端到端：提示词 → 落库 → 导出同结论
# ---------------------------------------------------------------------------


class TestEndToEndR214:
    @pytest.mark.parametrize("stored,full_title", [("3", "第三章 工程概况"), ("3.2", "2 进度计划")])
    def test_prompt_subheading_survives_landing_and_export(self, stored, full_title):
        from app.routers.export import _section_number_prefix
        from app.services.ai.heading_templates import build_subheading_rule
        from app.services.numbering import (
            renumber_section_body_subheadings,
            stored_id_to_display,
            stored_id_to_prefix,
        )

        prefix = stored_id_to_prefix(stored)
        assert prefix == _section_number_prefix(full_title)
        rule = build_subheading_rule(section_prefix=prefix)
        display = stored_id_to_display(stored)
        assert display in ("第三章", "2")
        # 模型按规则写出的第一个二级小标题，落库规范化后逐字不变
        heading = f"## {prefix}.1 总体安排"
        body = f"{heading}\n详见 {prefix}.1 节。"
        out, changes = renumber_section_body_subheadings(body, stored, 2)
        assert f"{prefix}.1 标题" in rule
        assert out == body
        assert not changes
