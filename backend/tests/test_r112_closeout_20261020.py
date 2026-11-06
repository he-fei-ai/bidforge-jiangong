# -*- coding: utf-8 -*-
"""R112 · 全仓遗留问题与后续建议收口 · 护栏（阶段一 A 类新增项）。

承接用户指令「全部遗留问题与后续建议按最优方案执行修复和加固处理」，
本轮按三阶段执行计划落地。本文件承载 **A-5（R72 压缩炸弹阈值环境变量化）**
的护栏，以及阶段二 B 类落地的护栏（见后续追加）。

A-5 护栏要点（零风险、纯加法式）：
  - 四个压缩炸弹闸门阈值默认值与旧硬编码逐字相同（2000 / 400MB / 200 / 64MB）
  - 环境变量可覆盖（BIDFORGE_MAX_ARCHIVE_*）
  - 非法值（非整数 / 负数 / 零）回退默认值（fail-soft）
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"


def _reload_file_parser():
    """重新加载 file_parser 模块（模块级常量在 import 期读取环境变量）。

    ⚠️ 必须**原地 reload**，不得 ``sys.modules.pop`` 后重新 import：
    pop + import 会得到第二个模块对象与**全新的 ParseError 类**，而进程内
    早先 ``from app.services.file_parser import ...`` 的引用仍指向旧模块 /
    旧类 —— 一旦本文件先于依赖方执行，就会出现「旧函数抛旧类 / 新 import
    拿新类」的错配（二进制拒绝用例抓不到异常、CSV 截断的 monkeypatch 失效）。
    原地 reload 保持模块对象与 ``__dict__`` 同一，既有函数引用继续读到新常量。
    """
    mod = importlib.import_module("app.services.file_parser")
    # reload 会重建模块内的类对象；把既有 ParseError 放回去，保证所有
    # 已导入的 `except ParseError` 旧引用与新抛出的异常仍是同一类。
    old_parse_error = mod.ParseError
    importlib.reload(mod)
    mod.ParseError = old_parse_error
    return mod


class TestCompressionBombThresholdsConfigurable:
    """A-5 · R72 G10 压缩炸弹阈值环境变量化护栏。"""

    def test_defaults_unchanged(self):
        """默认值与旧硬编码逐字相同：2000 / 400MB / 200 / 64MB。"""
        # 清掉环境变量保证走默认值
        for k in (
            "BIDFORGE_MAX_ARCHIVE_ENTRIES",
            "BIDFORGE_MAX_ARCHIVE_UNCOMPRESSED_BYTES",
            "BIDFORGE_MAX_COMPRESSION_RATIO",
            "BIDFORGE_MAX_ARCHIVE_ENTRY_BYTES",
        ):
            os.environ.pop(k, None)
        try:
            mod = _reload_file_parser()
        finally:
            os.environ.pop("BIDFORGE_MAX_ARCHIVE_ENTRIES", None)
            os.environ.pop("BIDFORGE_MAX_ARCHIVE_UNCOMPRESSED_BYTES", None)
            os.environ.pop("BIDFORGE_MAX_COMPRESSION_RATIO", None)
            os.environ.pop("BIDFORGE_MAX_ARCHIVE_ENTRY_BYTES", None)
        assert mod.MAX_ARCHIVE_ENTRIES == 2000
        assert mod.MAX_ARCHIVE_UNCOMPRESSED_BYTES == 400 * 1024 * 1024
        assert mod.MAX_COMPRESSION_RATIO == 200
        assert mod.MAX_ARCHIVE_ENTRY_BYTES == 64 * 1024 * 1024

    def test_env_override(self):
        """环境变量覆盖生效：设置后模块常量取环境变量值。"""
        os.environ["BIDFORGE_MAX_ARCHIVE_ENTRIES"] = "42"
        try:
            mod = _reload_file_parser()
        finally:
            os.environ.pop("BIDFORGE_MAX_ARCHIVE_ENTRIES", None)
        assert mod.MAX_ARCHIVE_ENTRIES == 42
        # 未设置的环境变量仍走默认值
        assert mod.MAX_COMPRESSION_RATIO == 200

    def test_invalid_env_falls_back_to_default(self):
        """非法环境变量值（非整数 / 零 / 负数）回退默认值（fail-soft）。"""
        for bad in ("abc", "0", "-5", ""):
            os.environ["BIDFORGE_MAX_ARCHIVE_ENTRIES"] = bad
            try:
                mod = _reload_file_parser()
            finally:
                os.environ.pop("BIDFORGE_MAX_ARCHIVE_ENTRIES", None)
            assert mod.MAX_ARCHIVE_ENTRIES == 2000, f"非法值 {bad!r} 未回退默认"

    def test_single_entry_read_limit_guard_docstring(self):
        """`_read_zip_entry_limited` 默认上限必须调用时解析（注释承诺的
        monkeypatch 生效契约不回归 —— 写成 `limit: int = MAX_ARCHIVE_ENTRY_BYTES`
        会在定义期固化，运行期调参失效）。"""
        fp = APP / "services" / "file_parser.py"
        src = fp.read_text(encoding="utf-8")
        assert "limit is None" in src, "limit 默认值必须在调用时解析（防定义期固化）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestSchemeProjectIdSingleSource:
    """B-4 · R54 F7 收敛确认：`SELECT project_id FROM schemes WHERE id=?`
    全仓零内联字面量副本（仅 facts_extractor.SCHEME_PROJECT_ID_SQL 定义处），
    且 `resolve_scheme_project_id` 服务层单一出口被消费方委托。"""

    SQL_SIG = "SELECT project_id FROM schemes WHERE id=?"

    def _all_py_files(self):
        for root, _, files in os.walk(APP):
            for fn in files:
                if fn.endswith(".py"):
                    yield Path(root) / fn

    def test_no_inline_literal_copy_in_app(self):
        """全仓 app/ 下 ast.Constant 字符串 0 命中该 SQL 字面量（除定义处）。"""
        import ast

        hits = []
        for p in self._all_py_files():
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if self.SQL_SIG in node.value:
                        is_def = p.name == "facts_extractor.py" and p.parent.name == "services"
                        if not is_def:
                            hits.append((str(p.relative_to(BACKEND)), node.value[:60]))
        assert hits == [], f"内联 SCHEME_PROJECT_ID_SQL 字面量副本：{hits}"

    def test_consumers_import_constant(self):
        """各消费方通过 import/`_fe.` 引用常量，不得出现 `db.execute("SELECT...` 拼接。"""
        for rel in (
            "routers/global_facts.py",
            "routers/bid_analysis.py",
            "routers/global_facts_docs.py",
            "routers/upload_outline.py",
            "routers/review.py",
            "routers/sections.py",
            "routers/sse_handlers.py",
            "services/placeholder_inventory.py",
        ):
            src = (APP / rel).read_text(encoding="utf-8")
            assert self.SQL_SIG not in src, f"{rel} 内联了 SCHEME_PROJECT_ID_SQL 字面量"

    def test_resolve_helper_delegated(self):
        """`resolve_scheme_project_id` 必须存在于 facts_extractor 且被委托消费
        （服务层单一出口不可悬空）。"""
        from app.services import facts_extractor

        assert callable(facts_extractor.resolve_scheme_project_id)
        # 至少两个跨模块消费方委托它（export / compliance / facts_builder …）
        import re

        delegated = 0
        for rel in (
            "routers/export.py",
            "routers/compliance.py",
            "services/facts_builder.py",
            "services/placeholder_inventory.py",
            "services/scheme_fingerprint.py",
        ):
            src = (APP / rel).read_text(encoding="utf-8")
            if "resolve_scheme_project_id" in src:
                delegated += 1
        assert delegated >= 2, f"resolve_scheme_project_id 委托方过少：{delegated}"


class TestBidJsonRepairDelegated:
    """B-3 · R58 确认：`_repair_json` 已委托统一 JSON 修复通道
    （collect_json_response_with_chat + 注入 ba.chat_with_fallback），
    保留确定性正则下界兜底；不直接 import provider_factory（保 monkeypatch）。"""

    SRC = "routers/bid_analysis.py"

    def test_repairs_via_unified_channel(self):
        """修复必须经 `collect_json_response_with_chat`（统一通道），
        不得退回自建劣化版（贪婪正则 + 无结构校验 + 硬编码提示词）。"""
        src = (APP / self.SRC).read_text(encoding="utf-8")
        # `_repair_json` 委托 `_collect_json_str`（薄壳）；统一通道调用点在后者。
        i_repair = src.find("async def _repair_json")
        i_collect = src.find("async def _collect_json_str")
        assert 0 <= i_collect < i_repair, "未找到 _collect_json_str（应定义于 _repair_json 之前）"
        seg = src[i_collect : i_collect + 1400]
        assert "collect_json_response_with_chat" in seg, "_collect_json_str 未委托统一 JSON 修复通道"
        assert "chat_with_fallback" in seg, "_collect_json_str 未注入 ba.chat_with_fallback"
        # _repair_json 必须调用 _collect_json_str（薄壳接线，docstring 较长故取大窗口）
        seg_repair = src[i_repair : i_repair + 1600]
        assert "_collect_json_str(" in seg_repair, "_repair_json 未委托 _collect_json_str（薄壳断链）"

    def test_deterministic_lower_bound_preserved(self):
        """确定性下界保留：AI 全挂时仍能「正则抠 JSON」救回可解析片段。"""
        src = (APP / self.SRC).read_text(encoding="utf-8")
        i = src.find("async def _repair_json")
        seg = src[i : i + 1200]
        assert "确定性下界" in seg or "re.search" in seg, "确定性正则兜底被移除"

    def test_no_direct_provider_factory_import_for_repair(self):
        """修复路径不直接 import provider_factory 的 chat（保既有 monkeypatch）：
        `collect_json_response` 内部引用 json_response 自己的导入，桩会失效。"""
        src = (APP / self.SRC).read_text(encoding="utf-8")
        i = src.find("async def _repair_json")
        seg = src[i : i + 1500]
        assert (
            "from app.services.ai import provider_factory" not in seg
        ), "_repair_json 直接 import provider_factory（monkeypatch 失效）"


class TestRendererAdjustmentNeedsProductSignoff:
    """B-1/B-2 · R64/R60 渲染器字号与卡片容量调整 —— 登记为**需产品确认的增强项**。

    背景：timeline/layout/comparison/gantt/labor 的纸面毫米低于下限
    （960px 宽度地板使日期/说明档 ≤1.80mm）。R64/R60 已落地「播报说实话」+
    折行决策，但**真正动字号/卡片容量会改变渲染产物尺寸（DOCX 插图），
    属行为变更**，文档明确标注「需产品确认字号调整范围」。本轮**刻意不落地**。

    护栏锁防误修：若后人把「字号自适应」当漏修补上，必须 bump
    `_RENDERER_VERSION`（否则旧渲染缓存键不失效、修复永不生效），
    且必须保持既有 `_WRAP_GAIN` / `_DATE_FONT_DESIGN` 契约。
    """

    def test_renderer_version_guard_docstring(self):
        """`_RENDERER_VERSION` 仍是唯一版本源（mermaid_renderer.py），
        改渲染行为必须 bump 它（R62 教训：不 bump 则旧缓存不失效）。"""
        from pathlib import Path

        mf = APP / "services" / "ai" / "mermaid_renderer.py"
        src = mf.read_text(encoding="utf-8")
        assert "_RENDERER_VERSION" in src, "mermaid_renderer 缺失 _RENDERER_VERSION（渲染缓存失效依赖它）"

    def test_timeline_row_contract_preserved(self):
        """timeline 折行契约（R64）不得被「字号调整」顺手改掉：
        `plan_timeline_rows` 目标函数仍以日期档字号纸面毫米为准。"""
        mt = APP / "services" / "ai" / "mermaid_timeline.py"
        src = mt.read_text(encoding="utf-8")
        assert "plan_timeline_rows" in src
        assert "paper_font_mm" in src, "timeline 折行决策不再用纸面毫米（R64 契约破坏）"
        assert "_DATE_FONT_DESIGN" in src, "timeline 常驻最小字号契约丢失"


class TestFrontendFactChangeListDeferred:
    """B-5 · R54 「哪几条事实变了」逐条可点清单 —— 前端增强，登记待排期。

    护栏锁：后端 `list_facts` 逐条 `updated_at` 已有（R54 F7 已收敛读路径），
    前端展示层增强不得伪装成后端判据修复回流（不得改 `list_facts` 返回契约）。
    """

    def test_list_facts_contract_unchanged(self):
        """`list_facts` 返回契约不得被前端增强回流改写（逐条可点清单是
        前端展示层的事，不改变后端返回结构）。"""
        gf = APP / "routers" / "global_facts.py"
        src = gf.read_text(encoding="utf-8")
        i = src.find("async def list_facts")
        assert i > 0, "未找到 list_facts"
        seg = src[i : i + 1200]
        # 不得出现把「逐条变更标记」写进 list_facts 返回体的形态
        assert "changed_facts" not in seg, "list_facts 被前端增强回流改写返回契约（B-5 登记项被误修）"
