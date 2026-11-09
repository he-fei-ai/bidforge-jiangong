# -*- coding: utf-8 -*-
"""R19x（2026-11-09）正文生成模块深度审计收口护栏。

覆盖本轮 6 项修复（另 3 项为源码棘轮/委托既有护栏覆盖）：
  FIX-1  正文首稿重试接永久错误闸（配额/认证类不重试，AI_RETRY_ON_QUOTA_ERROR 可退回）
  FIX-2  限流判定去裸子串（sse_handlers._is_rate_limit_text + provider_factory._status_from_error 词边界）
  FIX-3  task_status DB 分支 get_read_conn 保护（503 + 归因日志）
  FIX-4  档位字数批量 UPDATE 持 _db_write_lock + retry_db_op
  FIX-5  首稿重试退避响应停止/暂停闸门
  FIX-6  numeric_lock 围栏掩码委托 fence_common.fence_char_ranges（~~~ 与未闭合块）
  FIX-7  continue_max_tokens cap_factor 与字数容差同源（加法式参数）
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
SSE = APP_ROOT / "routers" / "sse_handlers.py"
PF = APP_ROOT / "services" / "ai" / "provider_factory.py"
CR = APP_ROOT / "services" / "content_runtime.py"
NL = APP_ROOT / "services" / "numeric_lock.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# =========================================================================
# A 组 —— FIX-2 行为实证（限流判定去裸子串）
# =========================================================================
class TestRateLimitPredicate:
    def test_word_boundary_429(self):
        from app.routers.sse_handlers import _is_rate_limit_text

        assert _is_rate_limit_text("http 429 too many requests")
        assert _is_rate_limit_text("rate limit exceeded")
        assert _is_rate_limit_text("RateLimitError".lower())
        assert _is_rate_limit_text("rate_limit_exceeded")
        assert _is_rate_limit_text("too many requests")
        assert _is_rate_limit_text("请稍后重试：限流")

    def test_bare_substring_false_positives_eliminated(self):
        from app.routers.sse_handlers import _is_rate_limit_text

        assert not _is_rate_limit_text("context 42900 tokens exceeded")
        assert not _is_rate_limit_text("http 42980 gateway")
        assert not _is_rate_limit_text("generate moderate the content")  # 旧 "rate"/"limit" 裸子串误命中
        assert not _is_rate_limit_text("word limit reached for section")  # 同上
        assert not _is_rate_limit_text("")

    def test_status_from_error_word_boundary(self):
        from app.services.ai.provider_factory import _status_from_error

        assert _status_from_error(RuntimeError("HTTP 429")) == 429
        assert _status_from_error(RuntimeError("HTTP/1.1 429 Too Many Requests")) == 429
        # 旧行为：这些都会被误判为 429
        assert _status_from_error(RuntimeError("context length 42900 tokens")) != 429
        assert _status_from_error(RuntimeError("HTTP 42980")) != 429
        assert _status_from_error(RuntimeError("HTTP 503")) == 503
        assert _status_from_error(RuntimeError("boom")) == 500


# =========================================================================
# B 组 —— FIX-1 行为实证（首稿重试永久错误闸）
# =========================================================================
class TestDraftQuotaGate:
    def _run_draft(self, monkeypatch, err: Exception, retries: int = 1):
        """直接驱动 _generate_first_draft 闭包不可行（嵌套在路由内），
        改为构造同形最小复刻：以源码锁 + 语义等价小样本共同钉住。
        这里走「整链最小桩」：monkeypatch chat_with_fallback 抛错，
        借助 sse_handlers 模块级 _is_quota_error / AI_RETRY_ON_QUOTA_ERROR
        验证判定函数本身与闸门配置读取，配合 C 组源码锁钉住调用点。"""
        import app.routers.sse_handlers as sh

        monkeypatch.setattr(sh, "AI_RETRY_ON_QUOTA_ERROR", False)
        return sh._is_quota_error(err)

    def test_quota_errors_classified(self, monkeypatch):
        assert self._run_draft(monkeypatch, RuntimeError("HTTP 402 insufficient balance"))
        assert self._run_draft(monkeypatch, RuntimeError("401 unauthorized"))
        assert self._run_draft(monkeypatch, RuntimeError("余额不足"))

    def test_rate_limit_not_in_quota_gate(self, monkeypatch):
        """429 必须保持退避重试，不得被永久错误闸吞掉。"""
        assert not self._run_draft(monkeypatch, RuntimeError("HTTP 429 too many requests"))

    def test_transient_not_in_quota_gate(self, monkeypatch):
        assert not self._run_draft(monkeypatch, RuntimeError("connection reset by peer"))
        assert not self._run_draft(monkeypatch, TimeoutError("timed out"))

    def test_source_lock_quota_gate_before_backoff(self):
        """闸门必须位于退避计算之前；退回旧行为的开关必须被读取。"""
        src = _src(SSE)
        assert "if not AI_RETRY_ON_QUOTA_ERROR and _is_quota_error(e):" in src
        assert "配额/认证类，不重试" in src
        # 闸门在 _generate_first_draft 体内（行序：闸门行 < 退避基数字）
        draft_start = src.index("async def _generate_first_draft(")
        seg = src[draft_start : src.index("async def _continue_if_needed(")]
        gate_at = seg.index("AI_RETRY_ON_QUOTA_ERROR and _is_quota_error(e)")
        backoff_at = seg.index("CONTENT_RATE_LIMIT_BACKOFF")
        assert gate_at < backoff_at


# =========================================================================
# C 组 —— FIX-3 / FIX-4 / FIX-5 行为与源码锁
# =========================================================================
class TestTaskStatusConnGuard:
    async def test_get_read_conn_failure_returns_503(self, monkeypatch):
        import app.routers.sse_handlers as sh

        monkeypatch.setattr(sh._tr, "_tasks", {}, raising=False)

        async def _boom():
            raise RuntimeError("db locked")

        monkeypatch.setattr(sh, "get_read_conn", _boom)
        with pytest.raises(Exception) as ei:
            await sh.task_status("no-such-task-r19x")
        # HTTPException(503)
        assert getattr(ei.value, "status_code", None) == 503

    def test_memory_branch_still_first(self):
        src = _src(SSE)
        assert "_tr._tasks.get(task_id)" in src
        assert "任务状态读取：获取读取连接失败" in src


class TestTierBudgetWriteGuard:
    def test_source_lock(self):
        src = _src(SSE)
        assert "_persist_tier_budget_once" in src
        # 持锁 + 重试必须同时在场（锚定 tier 分支行，避开请求体解析区）
        tier_at = src.index("if word_budget_tier is not None:")
        seg = src[tier_at : tier_at + 2600]
        assert "async with _db_write_lock" in seg
        assert "retry_db_op(_persist_tier_budget_once" in seg


class TestBackoffStopAwareness:
    def test_source_lock(self):
        src = _src(SSE)
        draft_start = src.index("async def _generate_first_draft(")
        seg = src[draft_start : src.index("async def _continue_if_needed(")]
        # 退避前过 wait_resume + is_stopped 双闸，退避后复检
        assert "await wait_resume(task_id)" in seg
        assert "跳过首稿重试退避" in seg
        assert "退避后终止首稿重试" in seg
        sleep_at = seg.index("await asyncio.sleep(backoff)")
        resume_at = seg.index("await wait_resume(task_id)")
        assert resume_at < sleep_at


# =========================================================================
# D 组 —— FIX-6 行为实证（numeric_lock 围栏掩码）
# =========================================================================
class TestNumericLockFenceMasking:
    def test_tilde_fence_masked(self):
        """旧实现 ~~~ 围栏完全不掩：图内数值被当正文数值。"""
        from app.services.numeric_lock import build_allowed_numbers, check_text

        allowed = build_allowed_numbers(["搭设高度不应超过 5m。"], [])
        text = "搭设高度 5m。\n~~~\ngraph TD\n  A[8m 跨距]\n~~~\n收尾。"
        # 8m 在 ~~~ 围栏内 → 不参与比对 → 不违规
        assert check_text(text, allowed).ok

    def test_backtick_fence_still_masked(self):
        from app.services.numeric_lock import build_allowed_numbers, check_text

        allowed = build_allowed_numbers(["搭设高度不应超过 5m。"], [])
        text = "搭设高度 5m。\n```mermaid\ngraph TD\n  A[8m 跨距]\n```\n收尾。"
        assert check_text(text, allowed).ok

    def test_inline_backticks_in_prose_not_fence(self):
        """行内 ``` 提及（非围栏行）：旧正则把两处提及间的**正文**整体掩掉，
        违规数值被隐藏；行级判据要求独立成行，正文数值照常比对。"""
        from app.services.numeric_lock import build_allowed_numbers, check_text

        allowed = build_allowed_numbers(["搭设高度不应超过 5m。"], [])
        text = "他说 ``` 不好；搭设高度 9m；又说 ``` 结束。"
        result = check_text(text, allowed)
        assert not result.ok
        assert "9m" in [v["raw"] for v in result.violations]

    def test_unclosed_fence_extends_to_end(self):
        """未闭合块：其后全部按代码处理（不得把围栏内数值当正文违规）。
        旧正则缺第二个 ``` 时不匹配 → 图内 8m 被当正文违规（误报）。"""
        from app.services.numeric_lock import build_allowed_numbers, check_text

        allowed = build_allowed_numbers(["搭设高度不应超过 5m。"], [])
        text = "搭设高度 5m。\n```mermaid\ngraph TD\n  A[8m 跨距]\n收尾说明 9m。\n"
        assert check_text(text, allowed).ok


# =========================================================================
# E 组 —— FIX-7 行为实证（cap_factor 同源）+ R76 棘轮回收
# =========================================================================
class TestContinueTokenCapFactor:
    def test_default_identical(self):
        from app.services.content_runtime import continue_max_tokens

        # 预算大且缺口小（floor 4000 不再兜底），cap_factor 差异可观测
        assert continue_max_tokens(word_budget=10000, wc=9000) == continue_max_tokens(
            word_budget=10000, wc=9000, cap_factor=1.1
        )

    def test_cap_factor_widens_budget(self):
        from app.services.content_runtime import continue_max_tokens

        tight = continue_max_tokens(word_budget=10000, wc=9000, cap_factor=1.1)
        wide = continue_max_tokens(word_budget=10000, wc=9000, cap_factor=1.5)
        assert wide > tight

    def test_dirty_cap_factor_falls_back(self):
        from app.services.content_runtime import continue_max_tokens

        base = continue_max_tokens(word_budget=1000, wc=0)
        assert continue_max_tokens(word_budget=1000, wc=0, cap_factor=0) == base
        assert continue_max_tokens(word_budget=1000, wc=0, cap_factor="x") == base  # type: ignore[arg-type]

    def test_call_site_uses_tolerance(self):
        src = _src(SSE)
        assert "cap_factor=1.0 + _cont_tol" in src


class TestNumericLockRatchet:
    def test_no_local_fence_regex(self):
        src = _src(NL)
        assert "_FENCE_RE" not in src
        assert "fence_char_ranges" in src

    def test_r76_consumer_list_contains_numeric_lock(self):
        test_src = (Path(__file__).resolve().parent / "test_content_fence_single_source_r76_20261013.py").read_text(
            encoding="utf-8"
        )
        assert '"services/numeric_lock.py"' in test_src


# =========================================================================
# F 组 —— 自证
# =========================================================================
class TestSelfProof:
    def test_fixes_present_in_source(self):
        pf_src = _src(PF)
        assert "_STATUS_429_RE" in pf_src
        cr_src = _src(CR)
        assert "cap_factor: float = 1.1" in cr_src

    def test_ast_parse_all_touched_files(self):
        for p in (SSE, PF, CR, NL):
            ast.parse(_src(p))
