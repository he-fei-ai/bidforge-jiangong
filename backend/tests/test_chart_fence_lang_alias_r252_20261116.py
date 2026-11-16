# -*- coding: utf-8 -*-
"""R252（2026-11-16）· 甘特图源码泄漏进导出 DOCX 根因修复护栏。

生产实证（截图第 33 页 · 机电安装施工组织设计）：AI 生成正文时把进度计划甘特图
写成 ````` ```gantt `````（而非 ````` ```mermaid `````），且首行从 ``title …``
起笔、漏掉 `gantt` 声明。三侧图表围栏白名单都只有 mermaid/chart-json/ai_image
⇒ ````` ```gantt ````` 被当普通代码块 → 前端 marked `<pre><code>`、后端
_add_code_block 把 Mermaid 源码**原样印进导出的 DOCX**（无图、无图号、无 AI
修复按钮）。即使标签修正为 mermaid，首行 `title` 又让 detect_mermaid_chart_type
返回空 → 整块跳过；而 PIL 解析器本就逐行扫描、容忍缺首行。

手法：行为实证（归一 / 解析 / 登记扫描）+ 反证（修前确实丢图）+ 行数不变棘轮
+ 幂等 + 既有行为保护（真实代码围栏不误伤）+ 前后端 alias/判据 parity 源码锁。
"""
from __future__ import annotations

from pathlib import Path

from app.routers._chart_pipeline import iter_inline_chart_fences
from app.services.chart_validators import (
    _looks_like_gantt_body,
    detect_mermaid_chart_type,
)
from app.services.content_blocks import _parse_content_blocks, normalize_chart_fence_shapes
from app.services.fence_common import (
    canonical_chart_fence_lang,
    is_chart_fence_lang,
)

# 截图取证形态（逐字骨架）：```gantt 围栏 + 首行 title（缺 gantt 声明）
GANTT_ALIAS_CONTENT = (
    "机电安装进度计划横道图如下：\n"
    "\n"
    "```gantt\n"
    "title 机电安装施工进度计划\n"
    "dateFormat YYYY-MM-DD\n"
    "axisFormat %m-%d\n"
    "section 施工准备\n"
    "现场勘测与技术交底 :2026-05-18, 5d\n"
    "材料设备招标 :2026-05-20, 7d\n"
    "section 系统调试\n"
    "单机调试 :2026-10-01, 15d\n"
    "竣工验收与移交 :2026-11-11, 8d\n"
    "```\n"
)


class TestFenceLangAlias:
    """围栏语言标签别名归一（后端唯一事实源）。"""

    def test_canonicalizes_chart_type_names(self):
        for alias in ("gantt", "flowchart", "graph", "pie", "timeline", "architecture",
                      "labor", "comparison", "layout", "sequenceDiagram", "mindmap"):
            assert canonical_chart_fence_lang(alias) == "mermaid", alias

    def test_keeps_canonical_and_real_code_langs(self):
        for lang in ("mermaid", "chart-json", "ai_image"):
            assert canonical_chart_fence_lang(lang) == lang
        for lang in ("python", "js", "javascript", "markdown", "text", "json", "sql", "bash"):
            assert canonical_chart_fence_lang(lang) == lang

    def test_is_chart_fence_lang_parity(self):
        assert is_chart_fence_lang("gantt") is True
        assert is_chart_fence_lang("```gantt") is False  # 传入应是首词，不是整行
        assert is_chart_fence_lang("flowchart") is True
        assert is_chart_fence_lang("python") is False
        assert is_chart_fence_lang("") is False

    def test_info_string_extra_tokens(self):
        assert canonical_chart_fence_lang("gantt title") == "mermaid"
        assert canonical_chart_fence_lang("  Gantt  ") == "mermaid"


class TestGanttBodyDetection:
    """首行 gantt 缺失时的结构特征兜底识别。"""

    def test_detects_missing_gantt_declaration(self):
        code = "\n".join(GANTT_ALIAS_CONTENT.split("\n")[3:-1])
        assert code.startswith("title")
        assert _looks_like_gantt_body(code) is True
        assert detect_mermaid_chart_type(code, default="") == "gantt"

    def test_no_false_positive_on_plain_text(self):
        assert _looks_like_gantt_body("普通段落：没有图表结构。") is False
        assert detect_mermaid_chart_type("普通段落文字", default="") == ""

    def test_known_keyword_wins(self):
        assert detect_mermaid_chart_type("flowchart TD\n A-->B", default="") == "flowchart"
        assert detect_mermaid_chart_type("gantt\ntitle x", default="") == "gantt"


class TestEndToEndBehavior:
    """归一 → 解析 → 登记 全链路行为实证。"""

    def test_alias_fence_rewritten_to_mermaid(self):
        norm = normalize_chart_fence_shapes(GANTT_ALIAS_CONTENT)
        assert "```gantt" not in norm
        assert "```mermaid" in norm

    def test_line_count_invariant(self):
        norm = normalize_chart_fence_shapes(GANTT_ALIAS_CONTENT)
        assert len(norm.split("\n")) == len(GANTT_ALIAS_CONTENT.split("\n"))

    def test_idempotent(self):
        once = normalize_chart_fence_shapes(GANTT_ALIAS_CONTENT)
        twice = normalize_chart_fence_shapes(once)
        assert once == twice

    def test_parse_emits_chart_block(self):
        blocks = _parse_content_blocks(GANTT_ALIAS_CONTENT)
        chart_blocks = [b for b in blocks if b.get("type") == "chart"]
        assert len(chart_blocks) == 1, blocks
        assert chart_blocks[0]["chart_type"] == "gantt"

    def test_registration_scan_reaches_chart(self):
        items = iter_inline_chart_fences(GANTT_ALIAS_CONTENT)
        assert items, "登记侧应能扫描到 gantt 别名围栏"
        lang, code, state, ordinal = items[0]
        assert lang == "mermaid"
        assert state in ("closed", "recovered")

    def test_real_code_fences_untouched(self):
        code = "```python\nprint('hello')\n```\n\n```js\nlet x = 1;\n```\n"
        assert normalize_chart_fence_shapes(code) == code

    def test_tilde_fence_alias(self):
        content = "~~~gantt\ntitle 计划\ndateFormat X\nsection A\n任务:t1,0,5d\n~~~\n"
        norm = normalize_chart_fence_shapes(content)
        assert "~~~gantt" not in norm
        assert "~~~mermaid" in norm

    def test_indented_fence_alias(self):
        content = "   ```gantt\ntitle 计划\ndateFormat X\nsection A\n任务:t1,0,5d\n   ```\n"
        norm = normalize_chart_fence_shapes(content)
        assert "```gantt" not in norm
        assert "   ```mermaid" in norm


class TestSourceLock:
    """源码锁：防止后续会话把三侧判据改回旧形态。"""

    def test_fence_common_has_alias_table(self):
        src = (Path(__file__).parents[1] / "app/services/fence_common.py").read_text(encoding="utf-8")
        assert "_CHART_FENCE_LANG_ALIASES" in src
        assert "def canonical_chart_fence_lang" in src
        assert '"gantt"' in src

    def test_content_blocks_wires_alias(self):
        src = (Path(__file__).parents[1] / "app/services/content_blocks.py").read_text(encoding="utf-8")
        assert "_normalize_chart_fence_lang_alias(lines)" in src

    def test_pipeline_uses_canonical_predicate(self):
        src = (Path(__file__).parents[1] / "app/routers/_chart_pipeline.py").read_text(encoding="utf-8")
        assert "is_chart_fence_lang as _is_chart_lang" in src
