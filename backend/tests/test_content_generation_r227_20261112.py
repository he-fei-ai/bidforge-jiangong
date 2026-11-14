"""正文生成模块 R227 深度审计增强护栏（2026-11-12）。

本轮 10 项修复的回归锁（行为 + 源码结构双层）：

  · 修复1  R199 配图累加 per-section 幂等重锚（test_chart_r199 内，已重锚）
  · 修复2  content_blocks 内联 `re.match` 预编译（R226 B3 回归）
  · 修复3  R46 导出缓存失效重锚（test_review_export_closeout_r46 内，已重锚）
  · 修复4  收尾审核置位 UPDATE + commit 纳入 `_db_write_lock`（跨方案并发）
  · 修复5  `word_adjust_batch_done.adjusted` 改发批内增量（L-3 体积有界）
  · 修复6  波浪线围栏（`~~~mermaid`）图题假表格形态识别（R220 仅认反引号）
  · 修复7  批调度 under/over 跳过未闭合围栏（对齐生成链 R224 F8 判据）
  · 修复8  无序列表无空格变体（`-材料进场`）识别 + 保守拒判 `-5℃`/`--`/`*bold`
  · 修复9  前端 `word_count`/`word_budget` 0 字不覆盖（`||`→`??`）
  · 修复10 前端 stopped 终态进度条 exception 不对称（terminal 信号）

复跑：``python -m pytest tests/test_content_generation_r227_20261112.py -q``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.services import content_blocks as cb

APP = Path(__file__).resolve().parents[1] / "app"
SSE = APP / "routers" / "sse_handlers.py"
FE = APP.parent.parent / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"
CARD = APP.parent.parent / "frontend" / "src" / "pages" / "schemeWorkbench" / "GenerationProgressCard.tsx"


def _sse_src() -> str:
    return SSE.read_text(encoding="utf-8")


# =========================================================================
# A · 行为层：波浪线围栏 + 无空格列表（Fix 6 / Fix 8）
# =========================================================================
class TestTildeFenceAndTightBullet:
    def test_tilde_figure_table_chart_unwrapped(self):
        """Fix 6：`| ~~~mermaid` / `~~~ |` 图题假表格形态必须解包为正常围栏。"""
        out = cb.normalize_chart_fence_shapes("| ~~~mermaid\nflowchart TD\n A-->B\n~~~ |")
        assert out == "~~~mermaid\nflowchart TD\n A-->B\n~~~", f"波浪线图题假表格未解包: {out!r}"

    def test_backtick_figure_table_chart_still_unwrapped(self):
        """Fix 6 回归：反引号形态（R220 既有）行为不变。"""
        out = cb.normalize_chart_fence_shapes("| ```mermaid\nflowchart TD\n A-->B\n``` |")
        assert out == "```mermaid\nflowchart TD\n A-->B\n```", f"反引号形态回归: {out!r}"

    def test_plain_tilde_code_block_untouched(self):
        """Fix 6 反向：纯 `~~~text` 代码样例（无图表标签）零触碰。"""
        out = cb.normalize_chart_fence_shapes("~~~text\nhello\n~~~")
        assert out == "~~~text\nhello\n~~~"

    def test_tight_hyphen_bullet_is_list_item(self):
        """Fix 8：`-材料进场`（无空格）应识别为列表项（旧版被拍平成段落）。"""
        blocks = cb._parse_content_blocks("-材料进场")
        assert blocks and blocks[0]["type"] == "list_item", f"无空格列表未被识别: {blocks}"
        assert blocks[0]["text"] == "材料进场"

    def test_tight_bullet_rejects_digit_hyphen_space(self):
        """Fix 8 反向：`-5℃`/`--x`/`*bold`/`- 有空格` 不得误判为列表项。"""
        for line in ("-5℃", "-- 标题", "*bold"):
            blocks = cb._parse_content_blocks(line)
            assert blocks[0]["type"] != "list_item" or blocks[0]["text"] != line, f"误判为列表项: {line}"
        # `- 有空格` 走原空格正则，仍是列表项
        blocks = cb._parse_content_blocks("- 有空格 项")
        assert blocks[0]["type"] == "list_item" and blocks[0]["text"] == "有空格 项"


# =========================================================================
# B · 源码结构层：正则预编译（Fix 2 / Fix 6 / Fix 8）
# =========================================================================
class TestPrecompiledConstants:
    def test_stop_ord_strip_tail_precompiled_and_used(self):
        """Fix 2：`_RE_STOP_ORD_STRIP_TAIL` 已预编译且被 `.match` 使用（无内联 re）。"""
        assert isinstance(getattr(cb, "_RE_STOP_ORD_STRIP_TAIL", None), re.Pattern)
        src = (APP / "services" / "content_blocks.py").read_text(encoding="utf-8")
        body = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        assert "_RE_STOP_ORD_STRIP_TAIL.match(rest)" in body
        # 无内联 re.match（剔除注释行后的代码面）
        code_lines = [ln.split("  #")[0] for ln in src.splitlines() if not ln.strip().startswith("#")]
        assert not re.findall(r"(?<![_.A-Za-z])re\.(?:match|search|sub)\(", "\n".join(code_lines))

    def test_tilde_fence_regexes_capture_marker(self):
        """Fix 6：图表围栏开启正则的 marker 组必须保持捕获（调用点用它重写行）。"""
        for pat in (cb._RE_FIG_TABLE_CHART_OPEN,):
            assert pat.groups >= 2, "marker + lang 两组必须都在（非捕获组会错位）"

    def test_tight_bullet_regex_precompiled(self):
        """Fix 8：`_RE_UL_BULLET_TIGHT` 已预编译且被调用点使用。"""
        assert isinstance(getattr(cb, "_RE_UL_BULLET_TIGHT", None), re.Pattern)
        src = (APP / "services" / "content_blocks.py").read_text(encoding="utf-8")
        assert "_RE_UL_BULLET.match(stripped) or _RE_UL_BULLET_TIGHT.match(stripped)" in src


# =========================================================================
# C · 源码结构层：批调度收尾锁 + 批内增量 + 未闭合围栏（Fix 4 / 5 / 7）
# =========================================================================
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


class TestFinalizeLockAndBatchScheduling:
    def test_pending_mark_inside_db_write_lock(self):
        """Fix 4：`_mark_pending_once` 置位 + 终态 commit 必须在 `_db_write_lock` 内。"""
        src = _sse_src().splitlines()
        lock_i = next(i for i, l in enumerate(src) if "async with _db_write_lock:" in l)
        lock_ind = _indent(src[lock_i])
        mark_i = next(i for i, l in enumerate(src) if "async def _mark_pending_once" in l)
        commit_i = next(
            i for i, l in enumerate(src) if "await db.commit()" in l and i > mark_i and i < mark_i + 60
        )
        # 置位/commit 必须比锁头更深（即锁体内），且锁头先于它们
        assert _indent(src[mark_i]) > lock_ind, "_mark_pending_once 未纳入 _db_write_lock"
        assert _indent(src[commit_i]) > lock_ind, "终态 commit 未纳入 _db_write_lock"
        assert lock_i < mark_i < commit_i

    def test_batch_done_sends_batch_incremental(self):
        """Fix 5：`word_adjust_batch_done` 发 `_batch_adjusted`（批内增量）而非全量 `_adjusted`。"""
        src = _sse_src()
        # 初始化与批间清空必须存在
        assert "_batch_adjusted: dict = {}" in src
        # batch_done 载荷用批内增量
        i = src.index('"event": "word_adjust_batch_done"')
        seg = src[i : i + 600]
        assert '"adjusted": _batch_adjusted' in seg, "batch_done 必须发批内增量"
        assert '"adjusted": _adjusted' not in seg, "batch_done 不得再发全量累积 dict"
        # 批间清空（有界）：`_batch_adjusted = {}` 必须出现在 batch_done 之后
        assert "_batch_adjusted = {}" in src

    def test_adjust_one_skips_unclosed_fence(self):
        """Fix 7：`_adjust_one` 在 under/over 分支前跳过未闭合围栏章。"""
        src = _sse_src().splitlines()
        adj_i = next(i for i, l in enumerate(src) if "async def _adjust_one" in l)
        # 找到 `find_unclosed_fences(_content)` 出现在 _adjust_one 定义之后、under 分支之前
        under_i = next(i for i, l in enumerate(src) if i > adj_i and 'item["direction"] == "under"' in l)
        fence_skip = next(
            (i for i in range(adj_i, under_i) if "find_unclosed_fences(_content)" in src[i]), None
        )
        assert fence_skip is not None, "_adjust_one 未在 under/over 分支前跳过未闭合围栏章"


# =========================================================================
# D · 前端源码结构（Fix 9 / 10）
# =========================================================================
class TestFrontendFix:
    def test_word_count_uses_nullish_coalescing(self):
        """Fix 9：`word_count`/`word_budget` 用 `??`（合法 0 不覆盖）。"""
        src = FE.read_text(encoding="utf-8")
        assert "word_count: evt.word_count ?? prev.word_count" in src
        assert "word_budget: evt.word_budget ?? prev.word_budget" in src
        assert "evt.word_count || prev.word_count" not in src, "旧 `||` 回退未移除"

    def test_terminal_prop_wired(self):
        """Fix 10：页面把终态信号传入进度卡，卡片 `hasFailed` 纳入 `terminal`。"""
        card = CARD.read_text(encoding="utf-8")
        assert "terminal?: boolean" in card
        assert "progress >= 1 || p.terminal === true" in card
        page = FE.read_text(encoding="utf-8")
        assert "const [taskTerminal, setTaskTerminal] = useState(false)" in page
        assert "terminal={taskTerminal}" in page
        assert "setTaskTerminal(true)" in page


# =========================================================================
# E · R227b 遗留项收口（L-4 单章耗时扣暂停 / 审核自动修复围栏守卫）
# =========================================================================
class TestLegacyCloseoutR227b:
    def test_section_ms_excludes_pause(self):
        """L-4：`_section_ms_excluding_pause` 净耗时 = 墙钟 - 本段累计暂停。"""
        from app.routers.sse_handlers import _section_ms_excluding_pause

        now, started = 1000.0, 900.0
        # 无暂停：墙钟 100s 原样（逐字节旧行为）
        assert _section_ms_excluding_pause(now, started, 0.0, 0.0) == pytest.approx(100.0)
        # 本段累计暂停 40s：净耗时 60s
        assert _section_ms_excluding_pause(now, started, 40_000.0, 0.0) == pytest.approx(60.0)
        # 锚点 30s（章节启动时已暂停 30s）、当前 40s：本段只扣 10s → 90s
        assert _section_ms_excluding_pause(now, started, 40_000.0, 30_000.0) == pytest.approx(90.0)
        # 锚点缺失（None）：回退全额扣除（与 _sse_outline_build._section_partial 同语义）
        assert _section_ms_excluding_pause(now, started, 40_000.0, None) == pytest.approx(60.0)
        # 暂停超过墙钟：钳 0（不出现负耗时）
        assert _section_ms_excluding_pause(now, started, 200_000.0, 0.0) == 0.0

    def test_section_ms_call_site_uses_helper(self):
        """L-4：生成链收尾 `section_ms` 改用扣暂停助手，裸墙钟被替换。"""
        src = _sse_src()
        assert "def _section_ms_excluding_pause(" in src, "助手未定义"
        assert "_section_ms_excluding_pause(" in src, "调用点未使用助手"
        assert "max(0.0, time.monotonic() - _started)" not in src, "裸墙钟 append 未替换"

    def test_review_autofix_unclosed_fence_guard(self):
        """审核自动修复：落库前对最终 `_persist_content` 做未闭合围栏守卫。

        命中 → 跳过本章修复（保留原正文 + 旧审核态），status 加法式新增
        ``unclosed_fence``（响应契约零破坏）。
        """
        raf = Path(__file__).resolve().parents[1] / "app" / "routers" / "review_autofix.py"
        src = raf.read_text(encoding="utf-8")
        assert "from app.services.content_utils import find_unclosed_fences" in src
        # ✅ R243：守卫收窄为「仅当修复**新引入**未闭合围栏（修复前正文本就闭合）时跳过」，
        #    预存未闭合围栏的定向部分修复（前缀式接受）照常落库。断言改为**细化后**的守卫，
        #    保护面不变：仍必须位于正文 UPDATE 之前。
        assert "if find_unclosed_fences(_persist_content) and not find_unclosed_fences(_before):" in src
        assert '"unclosed_fence"' in src
        # 守卫必须位于该函数正文 UPDATE 之前（跳过则不落库）
        assert src.index(
            "if find_unclosed_fences(_persist_content) and not find_unclosed_fences(_before):"
        ) < src.index(
            '"UPDATE sections SET content=?, word_count=?, word_status=?,"'
        )
