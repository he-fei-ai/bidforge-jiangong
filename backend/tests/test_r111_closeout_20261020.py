# -*- coding: utf-8 -*-
"""R111 护栏：P2×7 承接收口（低危项落地 + 高危项登记）。

- P2-1 保存配置去重提示（ai_config/config.py duplicate_hint，加法式不阻断）
- P2-2 死配置报告门槛透明化（provider_factory.report_dead_configs_at_startup）
- P2-5 导出缓存命中/未命中日志埋点（export.py 4 处 INFO）
- P2-6 dashscope 渠道卫生（image_engine 无 key 短路 + 4xx 确定性错误点名）
- P2-7 事实单段确定性永久错误不重试（facts_extractor 终止整段重试）

P2-3（字数 2× 截断）与 P2-4（SSE 进度跳变）属行为变更大项，**刻意登记不落地**
（本文件锁「未回流」判据，防后人误当漏修补上）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


# =====================================================================
# P2-1 · 保存配置去重提示
# =====================================================================


class TestConfigSaveDuplicateHint:
    SRC = "app/routers/ai_config/config.py"

    def test_duplicate_hint_key_in_response(self):
        """保存响应必须含 duplicate_hint 加法式键（不阻断、旧前端零影响）。"""
        src = _src(self.SRC)
        assert '"duplicate_hint": duplicate_hint' in src

    def test_hint_only_on_create_not_update(self):
        """仅新增路径提示（data.id 为空）；更新路径不提示（同 id 重存合法）。"""
        src = _src(self.SRC)
        assert "if not data.id:" in src
        # 回查 SQL 必须排除本条自身（id != ?），否则自匹配恒 1
        assert "id != ?" in src

    def test_no_blocking_on_duplicate(self):
        """重复绝不阻断保存（R111 纪律：去重是提示不是门禁）。"""
        src = _src(self.SRC)
        # 提示之后必须继续走 after_row / 审计 / return，不得 raise
        i = src.find("duplicate_hint = ")
        tail = src[i : i + 400]
        assert "duplicate_hint" in tail
        # 不得出现因重复而 400/409 的形态
        assert "duplicate_hint" not in tail.split("\nraise")[0] or "raise HTTPException" not in tail

    def test_triple_identity_includes_base_url(self):
        """去重身份 = provider + model + base_url（密钥除外）；密钥不进判据。"""
        src = _src(self.SRC)
        i = src.find("SELECT id FROM ai_config WHERE provider_name=? AND model=?")
        assert i > 0
        seg = src[i : i + 200]
        assert "base_url" in seg
        assert "api_key" not in seg


# =====================================================================
# P2-2 · 死配置报告门槛透明化
# =====================================================================


class TestDeadConfigReportTransparency:
    SRC = "app/services/ai/provider_factory.py"

    def test_low_sample_dead_config_named(self):
        """样本未达死配置门槛、仅靠确定性连击判死的配置必须被点名。"""
        src = _src(self.SRC)
        assert "_PROVIDER_DEAD_MIN_SAMPLES" in src
        i = src.find("本轮判死主要靠确定性 404 连击而非统计口径")
        assert i > 0, "门槛透明化 INFO 缺失（P2-2 接线断链）"

    def test_only_when_not_det_dead_and_low_sample(self):
        """点名条件 = 非确定性连击死 且 样本 < 死门槛（不得误报统计口径判死）。"""
        src = _src(self.SRC)
        i = src.find("if not det_dead and total < _PROVIDER_DEAD_MIN_SAMPLES:")
        assert i > 0

    def test_no_behavior_change(self):
        """纯可观测：报告函数体不得新增剔除/写库/冷却操作。"""
        tree = ast.parse(_src(self.SRC))
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "report_dead_configs_at_startup"
        )
        # 函数内除 logger/append/continue/return/for 外不得有写库/剔除调用
        bad_calls = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
                if name in {"execute", "executemany", "commit"} or name.startswith("_note_"):
                    bad_calls.add(name)
        assert not bad_calls, f"死配置报告出现写库/剔除调用: {bad_calls}"


# =====================================================================
# P2-5 · 导出缓存命中/未命中日志埋点
# =====================================================================


class TestExportCacheLogging:
    SRC = "app/routers/export.py"

    def test_docx_hit_and_miss_logged(self):
        src = _src(self.SRC)
        assert src.count("导出缓存命中") >= 2, "DOCX/PDF 命中埋点缺失"
        assert src.count("导出缓存未命中") >= 2, "DOCX/PDF 未命中埋点缺失"

    def test_logging_is_info_level(self):
        """缓存命中/未命中是正常控制流，必须 INFO（不得 WARNING，R11 先例）。"""
        src = _src(self.SRC)
        for marker in ("导出缓存命中", "导出缓存未命中"):
            i = 0
            while True:
                i = src.find(marker, i)
                if i < 0:
                    break
                # 前 200 字符内必须是 logger.info（不得 WARNING）
                seg = src[max(0, i - 200) : i]
                assert (
                    "logger.info" in seg and "logger.warning" not in seg[seg.rfind("logger") :]
                ), f"{marker} 埋点级别应为 INFO"
                i += 1

    def test_hit_log_before_atomic_trace_write(self):
        """命中埋点必须在「轮次 + 留痕」事务之前（先记录命中态再落审核）。

        ✅ R176 · D2-3（2026-11-04）：命中路径改为
        ``_bump_export_round_and_trace`` 单事务写入，旧的直接
        ``_record_export_review_trace`` 调用点已并入该 helper。
        """
        src = _src(self.SRC)
        i = 0
        while True:
            hit = src.find("导出缓存命中", i)
            if hit < 0:
                break
            trace = src.find("_bump_export_round_and_trace", hit)
            assert trace > hit, "命中埋点必须在留痕事务调用之前"
            i = hit + 1


# =====================================================================
# P2-6 · dashscope 渠道卫生
# =====================================================================


class TestDashscopeChannelHygiene:
    SRC = "app/services/ai/image_engine.py"

    def test_no_key_short_circuit(self):
        """无 API Key 的渠道候选必须请求前短路（不发必然 400/401 的请求）。"""
        src = _src(self.SRC)
        i = src.find("无 API Key，跳过（不发请求）")
        assert i > 0
        # 短路必须在适配器调用之前
        assert i < src.find("_adapter.generate")

    def test_4xx_named_as_config_error(self):
        """4xx 渠道失败必须点名「确定性渠道配置错误」（与 5xx 瞬态分流）。"""
        src = _src(self.SRC)
        assert "确定性渠道配置" in src
        assert "http 400" in src.lower()

    def test_behavior_chain_unchanged(self):
        """降级链行为不变：4xx/无 key 仍 continue 下一候选（不中止整条链）。"""
        src = _src(self.SRC)
        # 两处跳过分支都必须以 continue 收尾
        assert src.count("continue") >= 3


# =====================================================================
# P2-7 · 事实单段确定性永久错误不重试
# =====================================================================


class TestFactsChunkPermanentNoRetry:
    SRC = "app/services/facts_extractor.py"

    def test_non_retryable_terminates_chunk_retry(self):
        """确定性永久错误（401/402/403/404 等）必须终止整段重试（break）。"""
        src = _src(self.SRC)
        i = src.find("不再整段重试")
        assert i > 0
        # break 必须在 is_non_retryable_error 判据之后
        assert "is_non_retryable_error" in src
        j = src.find("if _permanent:")
        assert j > 0
        assert "break" in src[j : j + 400]

    def test_uses_provider_factory_single_source(self):
        """判据必须委托 provider_factory.is_non_retryable_error（单一事实源）。"""
        src = _src(self.SRC)
        assert "from app.services.ai.provider_factory import is_non_retryable_error" in src

    def test_fail_soft_on_predicate_self_failure(self):
        """判据自身故障不得中断提取（fail-soft：_permanent 回落 False 走旧退避路径）。"""
        src = _src(self.SRC)
        i = src.find("_permanent = is_non_retryable_error(e)")
        assert i > 0
        seg = src[i : i + 500]
        assert "except Exception" in seg and "_permanent = False" in seg

    def test_rate_limit_not_in_permanent_set(self):
        """429 限流不得进永久错误判定**逻辑**（退避后仍可成功，保留既有退避路径）。

        判据纪律（§5.14 同源 + 本轮第二坑）：
          - 必须锚定**实际判定代码**，不得扫全函数文本（docstring 里
            「429 不在此列」的说明文字会让纯文本判据自咬）；
          - AST 判据也**不得含 docstring**：`ast.walk` 会把函数的 docstring
            （Expr/Constant 子节点）扫进来，说明文字含 "HTTP 429" 同样自咬。
            正确做法：先 `ast.parse` 后剥掉函数体首个 docstring 节点，再
            收集 Constant 字符串（与 R75c 「docstring 剥掉再扫」同族）。
        """
        src_pf = _src("app/services/ai/provider_factory.py")
        tree = ast.parse(src_pf)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "is_non_retryable_error"
        )
        # 剥掉 docstring（函数体首节点）与注释级字符串，只留判定逻辑字面量
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, (ast.Constant, ast.JoinedStr)):
            body = body[1:]
        strs = [
            n.value for sub in body for n in ast.walk(sub) if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        assert not any("429" in s for s in strs), "429 被写入永久错误判定字面量（回归 R109 退避路径）"
        # 行为实证：429 必须判可重试；401/402/403/404 必须判永久（R109 判据不回流）
        from app.services.ai.provider_factory import is_non_retryable_error

        assert not is_non_retryable_error("HTTP 429 rate limit")
        for code in (
            "HTTP 401: invalid token",
            "HTTP 402 Insufficient Balance",
            "HTTP 403 forbidden",
            "HTTP 404 InvalidEndpointOrModel.NotFound",
        ):
            assert is_non_retryable_error(code), f"{code} 未判永久（R109 判据回流）"


# =====================================================================
# 高危项登记锁（P2-3 / P2-4 刻意不落地，防误修）
# =====================================================================


class TestDeferredItemsStayRegistered:
    """P2-3 字数 2× 截断 / P2-4 SSE 进度跳变：行为变更大，本轮**只登记不落地**。

    锁「未回流」：若后人误把这两项当漏修补上（截断逻辑 / 进度公式改写），
    须先显式改本护栏——接线是有意识决策。
    """

    def test_no_word_truncation_in_content_standard(self):
        """正文侧不得新增字数 2× 硬截断（R111 未落地项）。"""
        src = _src("app/services/content_standard.py")
        # 截断 = 对 content 切片写回；判据「content[:N] 赋值」不得出现
        assert "content[:" not in src, "字数截断逻辑回流（P2-3 登记项被误修）"

    def test_sse_progress_formula_untouched(self):
        """SSE 正文进度计算仍走既有 done/total 线性口径（P2-4 登记项未落地）。"""
        src = _src("app/routers/sse_handlers.py")
        # 既有口径锚：章节完成比例 = 已完成 / 总数（不得出现 95→100 特判跳变代码）
        assert "0.95" not in src and "95%→100" not in src, "SSE 进度 95% 特判回流（P2-4 登记项被误修）"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
