"""目录生成 · 长方案分步链路「分批并发」回归测试（2026-09-20 增强）。

背景：旧实现逐章子目录**纯串行**（30 章 × ~45s ≈ 22 分钟），是长方案
「看起来卡死」的最大来源（深度审查报告 5.2 的 P0 建议）。现改为
批内并发（OUTLINE_CHAPTER_CONCURRENCY）、批间串行（保留跨章去重上下文）。

锁定的不变量：
1. **暂停闸门在并发信号量之前**（与 bid_analysis.run_with_pause_gate 同源）——
   暂停期间在途章不得占住许可；
2. 结果按 i 归位：asyncio.gather 保序（非 as_completed），进度 done 递增、章序不乱；
3. 失败语义：首次失败重试 1 次（重试前再过闸门），两次均失败 → "failed"；
4. 停止语义：is_stopped → status="stopped"，不再调用 AI；
5. 并发数配置化：OUTLINE_CHAPTER_CONCURRENCY 来自 Settings.outline_chapter_concurrency，
   <=0 视为 1（串行）。
"""
import asyncio
import inspect

import pytest
from app.config import Settings
from app.routers import sse_handlers as sh


async def _push_stats():
    return None


# ============================================================
# 行为级：_fetch_chapter_children 执行单元
# ============================================================
class TestFetchChapterChildren:
    def _mk(self, monkeypatch, *, ai_results, stopped_fn=None, wait_gate=None):
        """公共脚手架：monkeypatch 模块级 wait_resume / is_stopped / collect_json_response。

        ai_results: list —— 每次被消费一项（值=返回 outline；Exception=抛出）。
        fake 返回 (obj, raw) 元组，与真实 collect_json_response 契约一致。
        """
        calls: list = []

        def fake_collect(prompt, validate_fn, timeout=None, **kwargs):
            # ✅ 2026-09-21（B/E 回归锁）：逐章子目录调用必须带
            #    json_mode（降解析失败放大）与 scene 标记（审计归因）
            assert kwargs.get("json_mode") is True
            assert kwargs.get("scene") == "outline_sublevel"
            calls.append(prompt)
            result = ai_results.pop(0) if ai_results else Exception("exhausted")
            if isinstance(result, Exception):
                raise result

            async def _coro():
                return result, "raw"
            return _coro()

        async def fake_wait_resume(task_id):
            if wait_gate is not None:
                await wait_gate()
            return None

        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        monkeypatch.setattr(sh, "wait_resume", fake_wait_resume)
        monkeypatch.setattr(
            sh, "is_stopped", stopped_fn or (lambda task_id: False))
        return calls

    async def test_ok_returns_children(self, monkeypatch):
        outline = [{"title": "1.1", "children": []}]
        self._mk(monkeypatch, ai_results=[{"outline": outline}])
        status, children = await sh._fetch_chapter_children(
            0, {"title": "第1章"}, sub_prompt=[{"role": "system", "content": "x"}],
            task_id="t", sem=None, validate_fn=lambda o: [],
            timeout=1, push_stats=_push_stats)
        assert status == "ok"
        assert children == outline

    async def test_empty_outline_coerced_to_list(self, monkeypatch):
        self._mk(monkeypatch, ai_results=[{"outline": None}])
        status, children = await sh._fetch_chapter_children(
            0, {"title": "x"}, sub_prompt=[], task_id="t", sem=None,
            validate_fn=lambda o: [], timeout=1, push_stats=_push_stats)
        assert status == "ok" and children == []

    async def test_retry_once_then_ok(self, monkeypatch):
        outline = [{"title": "2.1", "children": []}]
        self._mk(monkeypatch, ai_results=[Exception("boom"), {"outline": outline}])
        status, children = await sh._fetch_chapter_children(
            1, {"title": "第2章"}, sub_prompt=[], task_id="t", sem=None,
            validate_fn=lambda o: [], timeout=1, push_stats=_push_stats)
        assert status == "ok" and children == outline

    async def test_double_fail_is_failed(self, monkeypatch):
        calls = self._mk(monkeypatch, ai_results=[Exception("e1"), Exception("e2")])
        status, children = await sh._fetch_chapter_children(
            0, {"title": "x"}, sub_prompt=[], task_id="t", sem=None,
            validate_fn=lambda o: [], timeout=1, push_stats=_push_stats)
        assert status == "failed" and children == []
        assert len(calls) == 2, "必须且仅重试一次"

    async def test_stopped_before_start_skips_ai(self, monkeypatch):
        calls = self._mk(monkeypatch, ai_results=[], stopped_fn=lambda tid: True)
        status, children = await sh._fetch_chapter_children(
            0, {"title": "x"}, sub_prompt=[], task_id="t", sem=None,
            validate_fn=lambda o: [], timeout=1, push_stats=_push_stats)
        assert status == "stopped" and children == []
        assert calls == [], "已停止后不得再发起 AI 调用"

    async def test_stopped_while_queuing_skips_ai(self, monkeypatch):
        """排队等到许可后发现已停止 → 不启动 AI（信号量后的复检）。"""
        flag = {"stopped": False}
        calls = self._mk(monkeypatch, ai_results=[{"outline": []}],
                         stopped_fn=lambda tid: flag["stopped"])
        sem = asyncio.Semaphore(1)
        # 先占住唯一许可，让目标协程停在 async with sem 上；随后置停止再放行
        async def _holder():
            async with sem:
                await asyncio.sleep(0.02)
                flag["stopped"] = True
        holder = asyncio.create_task(_holder())
        await asyncio.sleep(0.005)
        t = asyncio.create_task(sh._fetch_chapter_children(
            0, {"title": "x"}, sub_prompt=[], task_id="t", sem=sem,
            validate_fn=lambda o: [], timeout=1, push_stats=_push_stats))
        status, _ = await t
        await holder
        assert status == "stopped"
        assert calls == [], "停止后不得启动 AI 调用"

    async def test_pause_gate_does_not_hold_semaphore(self, monkeypatch):
        """✅ 核心不变量：暂停闸门在信号量之前 —— 暂停挂起期间许可不被占住。

        同源教训（sse_handlers.guarded_gen / bid_analysis.run_with_pause_gate）：
        旧式「先 async with sem 再 wait_resume」在暂停期间把许可全部占住，
        饿死同批其余章节。
        """
        release = asyncio.Event()

        async def gate():
            await release.wait()

        self._mk(monkeypatch, ai_results=[{"outline": []}], wait_gate=gate)
        sem = asyncio.Semaphore(1)
        t = asyncio.create_task(sh._fetch_chapter_children(
            0, {"title": "x"}, sub_prompt=[], task_id="t", sem=sem,
            validate_fn=lambda o: [], timeout=1, push_stats=_push_stats))
        await asyncio.sleep(0.02)
        assert not sem.locked(), "暂停期间许可被占住（闸门必须排在信号量之前）"
        release.set()
        status, _ = await t
        assert status == "ok"


# ============================================================
# 源码级护栏：编排语义
# ============================================================
class TestStepwiseOrchestrationGuards:
    def test_gate_precedes_semaphore(self):
        src = inspect.getsource(sh._fetch_chapter_children)
        # 跳过 docstring（其中引用了旧式反例「先 async with sem 再 wait_resume」）
        body = src[src.index('"""', src.index('"""') + 3) + 3:]
        assert body.index("await wait_resume(task_id)") < body.index("async with sem:"), \
            "暂停闸门必须排在并发信号量之前"

    def test_gather_not_as_completed(self):
        """结果必须按 i 归位（gather 保序），不得改用 as_completed 完成序归位。"""
        src = inspect.getsource(sh.generate_outline)
        assert "asyncio.gather(" in src
        assert "as_completed" not in src

    def test_results_placed_by_index(self):
        src = inspect.getsource(sh.generate_outline)
        assert "results[j]" in src, "批内结果必须按章序号归位"

    def test_batch_uses_pause_gate(self):
        src = inspect.getsource(sh.generate_outline)
        seg = src[src.index("for batch_start in range("):]
        gate_at = seg.index("await wait_resume(task_id)")
        stop_at = seg.index("if is_stopped(task_id)", gate_at)
        gather_at = seg.index("results = await asyncio.gather(")
        assert gate_at < stop_at < gather_at, \
            "批间循环必须先过暂停闸门 + 停止检查，再发起并发 gather"


# ============================================================
# 配置化：并发数来自 Settings
# ============================================================
class TestConcurrencyConfig:
    def test_settings_field_default(self):
        # ✅ 2026-09-22（O7）：默认 3 → 2（并发峰值下降换 429 限流风险下降，
        #    调用数只多 ~11%、墙钟慢 ~1.75 倍；旧行为可用环境变量恢复）。
        assert Settings().outline_chapter_concurrency == 2

    def test_module_constant_follows_settings(self):
        assert sh.OUTLINE_CHAPTER_CONCURRENCY == max(
            1, int(Settings().outline_chapter_concurrency))

    def test_non_positive_clamped_to_serial(self):
        s = Settings(outline_chapter_concurrency=0)
        assert max(1, int(s.outline_chapter_concurrency)) == 1
