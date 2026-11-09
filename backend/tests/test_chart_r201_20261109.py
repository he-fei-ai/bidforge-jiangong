# -*- coding: utf-8 -*-
"""R201 图表模块遗留收尾护栏（2026-11-09）。

承接 R199/R200 登记清单，把**证伪项与设计取舍**从"文字登记"升级为**可执行棘轮**
（防止后续会话把刻意决策"顺手优化"掉），并锁定本轮注释一致性：

  A. 设计决策锁：
     - ``_validate_inline_chart``：校验器自身异常 → WARNING + 按通过处理
       （宁保留正文不误删；异常不再静默）；
     - ``build_chart_quota_hint``：word_budget < _CHART_MIN_WORDS 的短章建议
       分支保留（生产不可达但函数级行为有测试锁定，勿删）；
     - ``_figure_chapter_num``：全零计数器回退 1（无章节时归 ch1 命名空间）；
     - AI 配图失败「不占号、不写红字占位」（交付观感取舍，与 chart 占位语义
       不同是**有意**为之，可观测由 ai_image_pending / X-Chart-Render-Stats 承担）；
  B. 注释一致性锁：`_add_inline_chart_from_bytes` 的可用性判据注释必须与
     R200 之后的 probe 口径一致（不得仍称"调用方已用 _chart_ok 先行判定"）。
  C. G4 前端组件级用例存在性锁（SectionLogList 图表 Tag 展示）。

⚠️ 只跑本文件：
    python -m pytest tests/test_chart_r201_20261109.py -q
"""

from pathlib import Path

import app.routers.export  # noqa: F401  — 循环引用顺序敏感：export 必须先于 _export_docx_parts（R195 教训）

APP = Path(__file__).resolve().parents[1] / "app"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


class TestDesignDecisionLocks:
    def test_validator_exception_is_warned_and_passed(self):
        src = (APP / "routers" / "_chart_pipeline.py").read_text(encoding="utf-8")
        assert "校验器异常，按通过处理" in src, "校验器异常必须留 WARNING（不得静默）"
        assert "is_valid = True" in src, "校验器异常按通过处理（宁保留正文不误删）——刻意决策"

    def test_quota_hint_short_section_branch_kept(self):
        src = (APP / "routers" / "_chart_pipeline.py").read_text(encoding="utf-8")
        i = src.index("def build_chart_quota_hint")
        seg = src[i : i + 3000]
        assert "_CHART_MIN_WORDS" in seg, "短章不配图建议分支必须保留（函数级行为有测试锁定）"

    def test_figure_chapter_num_fallback_to_one(self):
        from app.routers._export_docx_parts import _figure_chapter_num

        class _Gen:
            counters = [0, 0, 0]

        assert _figure_chapter_num(_Gen()) == 1, "无有效章节计数器时归 ch1 命名空间"
        src = (APP / "routers" / "_export_docx_parts.py").read_text(encoding="utf-8")
        i = src.index("def _figure_chapter_num")
        seg = src[i : i + 1500]
        assert "return 1" in seg

    def test_ai_image_failure_no_placeholder_no_number(self):
        src = (APP / "routers" / "export.py").read_text(encoding="utf-8")
        assert "不做红字占位" in src, "AI 配图失败的红字占位取舍必须在注释中显式声明"
        i = src.index("不做红字占位")
        seg = src[max(0, i - 900) : i + 400]
        assert "不占用图号" in seg, "AI 配图失败必须明确不占用图号"


class TestCommentConsistency:
    def test_chart_insert_docstring_mentions_probe(self):
        src = (APP / "routers" / "_export_docx_parts.py").read_text(encoding="utf-8")
        i = src.index("def _add_inline_chart_from_bytes")
        seg = src[i : i + 2600]
        assert "_chart_probe" in seg, "可用性判据注释须与 R200 probe 口径一致"
        assert "已用 _chart_ok" not in seg, "旧口径（_chart_ok 先行判定）注释已过时，必须更新"


class TestFrontendComponentCoverage:
    def test_section_log_list_chart_test_exists(self):
        p = FRONTEND / "tests" / "sectionLogListChartsR201.test.tsx"
        assert p.exists(), "G4 组件级用例缺失（SectionLogList 图表 Tag 展示）"
        src = p.read_text(encoding="utf-8")
        assert "配图" in src and "已删" in src
