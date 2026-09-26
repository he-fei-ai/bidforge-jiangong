"""正文生成模块第十一轮审查回归（2026-09-19）

覆盖四项修复的行为与接线：
- BUG-1  全文一致性管线吞掉 CancelledError → 用户在一致性阶段停止（或同方案
         新任务注册时防僵尸 request_control("stop") 取消本管线）后，任务继续
         走完 completed 收尾（schemes.status='审核中' + review_status='pending'
         + completed 事件）。修复：取消必须 re-raise，event_stream 的
         CancelledError 分支才能正确落入 stopped 终态。
- BUG-2  GET /sections/quality 在事件循环内同步跑全方案正则扫描（百章 × 数千字
         可达数秒）→ 阻塞所有并发请求。修复：整体挪到 asyncio.to_thread。
- BUG-3  正文 SSE event_queue 无界 → 客户端断开后生产者无界积压。修复：
         maxsize=1000 + 所有生产者阻塞式 await put（断开时被兜底 cancel 打断）。
- BUG-4  续写轮 AI 失败 / 产出被去重丢弃只记日志，前端无法区分「要点不足」与
         「续写失败没补上」。修复：_continue_failed_flag 纯函数判定 +
         section_done 携带 continue_failed + 前端日志标记。
"""
import asyncio
import inspect

import pytest

import app.routers.sse_handlers as sh
from app.routers.sse_handlers import _continue_failed_flag
from app.services.content_utils import WORD_UNDER_RATIO


# ---------------------------------------------------------------------------
# BUG-4：续写失败信号纯函数
# ---------------------------------------------------------------------------
class TestContinueFailedFlag:

    def test_no_failure_never_flags(self):
        # 从未发生过续写失败 → 恒 False（"要点不足写不满"由 word_status 表达）
        assert _continue_failed_flag(False, 100, 1500) is False
        assert _continue_failed_flag(False, 0, 1500) is False

    def test_failed_and_still_under_flags(self):
        budget = 1500
        under_line = int(budget * WORD_UNDER_RATIO)  # 1200
        assert _continue_failed_flag(True, under_line - 1, budget) is True
        assert _continue_failed_flag(True, 0, budget) is True

    def test_failed_but_recovered_no_flag(self):
        # 中途失败但后续轮补足：字数已达标 → 不上报
        budget = 1500
        assert _continue_failed_flag(True, int(budget * WORD_UNDER_RATIO), budget) is False
        assert _continue_failed_flag(True, 1500, budget) is False

    def test_dirty_values_safe(self):
        # 脏预算/脏字数不抛异常，走安全回退
        assert _continue_failed_flag(True, 100, 0) is False
        assert _continue_failed_flag(True, 100, None) is False
        assert _continue_failed_flag(True, None, 1500) is True
        assert _continue_failed_flag(True, "abc", 1500) is True
        assert _continue_failed_flag(False, "abc", "xyz") is False


# ---------------------------------------------------------------------------
# BUG-1：一致性管线取消必须传播（源码接线防护）
# ---------------------------------------------------------------------------
class TestConsistencyPipelineCancellation:
    def _pipeline_src(self) -> str:
        src = inspect.getsource(sh.generate_content)
        i = src.index("async def _run_consistency_pipeline(")
        # ✅ 稳健切法（2026-09-22）：旧实现取固定 6000 字符窗口，往该闭包里新增
        #    几行（如扫描缓存字段 / 修复参数）就会把尾部的 except 块挤出窗口，
        #    护栏误报「没有 re-raise」。改为取该闭包起的**全部**剩余源码：
        #    第一个 except asyncio.CancelledError 仍落在本管线内（gen_one 等
        #    的 except 都在它之前定义）。
        return src[i:]

    def test_cancelled_error_is_reraised(self):
        src = self._pipeline_src()
        i = src.index("except asyncio.CancelledError:")
        tail = src[i:i + 1600]
        assert re_raise_in_except(tail), (
            "_run_consistency_pipeline 的 except asyncio.CancelledError 分支必须 "
            "re-raise —— 吞取消会让「用户停止/防僵尸替换」后的一致性阶段误判 "
            "completed（schemes.status 被改、completed 事件误发）")


def re_raise_in_except(block: str) -> bool:
    """在 except 块文本中确认出现裸 raise（防护未来重构再次吞取消）。"""
    return any(
        line.strip() == "raise"
        for line in block.splitlines()
    )


# ---------------------------------------------------------------------------
# BUG-3：SSE 事件队列有界化（源码接线防护）
# ---------------------------------------------------------------------------
class TestSseQueueBounded:

    def test_event_queue_has_maxsize(self):
        src = inspect.getsource(sh.generate_content)
        assert "asyncio.Queue(maxsize=1000)" in src, (
            "正文 event_queue 必须有界（maxsize=1000）——无界队列在客户端断开后"
            "被生产者无界积压")

    def test_no_put_nowait_on_event_queue(self):
        src = inspect.getsource(sh.generate_content)
        # event_queue 的所有生产者必须用阻塞式 await put（断开时被 cancel 正确打断）；
        # put_nowait 在有界队列上会抛 QueueFull 炸掉章节任务。
        assert "event_queue.put_nowait(" not in src, (
            "event_queue 禁止 put_nowait：有界队列满时抛 QueueFull 会中断章节任务，"
            "应统一 await event_queue.put(...)")
        # 哨兵投递也必须是 await（取消场景由取消传播保证不阻塞）
        assert "await event_queue.put(DONE_SENTINEL)" in src


# ---------------------------------------------------------------------------
# BUG-4：续写失败信号接线（返回值 / 消费点 / 事件字段）
# ---------------------------------------------------------------------------
class TestContinueFailureWiring:

    def test_continue_fn_returns_tuple(self):
        src = inspect.getsource(sh.generate_content)
        assert "tuple[str, bool]" in src, (
            "_continue_if_needed 签名必须返回 (正文, 续写失败标志)")
        assert "return result, cont_failed" in src

    def test_gen_one_consumes_flag_and_emits(self):
        src = inspect.getsource(sh.generate_content)
        # 两个调用方（continue 模式 / 正常模式）都解包二元组
        assert src.count("result, cont_failed = await _continue_if_needed(") == 2, (
            "continue 模式与正常模式两处调用都必须接收 cont_failed")
        # section_done 事件携带信号
        assert '"continue_failed": cont_failed' in src

    def test_flag_gated_by_under_ratio(self):
        # 判定必须收口在纯函数（与 WORD_UNDER_RATIO 同口径，防两处漂移）
        assert "cont_failed = _continue_failed_flag(cont_failed, wc, word_budget)" in (
            inspect.getsource(sh.generate_content))


# ---------------------------------------------------------------------------
# BUG-2：quality 全量扫描移出事件循环（源码 + 行为回归）
# ---------------------------------------------------------------------------
class TestSectionsQualityOffloop:
    def test_scan_runs_in_worker_thread(self):
        import app.routers.sections as sec_mod
        src = inspect.getsource(sec_mod.sections_quality)
        assert "asyncio.to_thread(_scan)" in src, (
            "sections_quality 的正则全量扫描必须放线程池，避免阻塞事件循环")


@pytest.mark.asyncio
async def test_sections_quality_route_still_works(db_conn):
    """to_thread 改造后的行为回归：命中项与空库场景结果不变。"""
    from app.routers.sections import sections_quality

    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
        " sort_order, status, content) VALUES (?,?,?,?,?,?,?,?,?)",
        ("s_a", "sc1", "p1", "", "第一章 工程概况", 1, 0, "generated",
         "咱们今天搞定基础施工。"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
        " sort_order, status, content) VALUES (?,?,?,?,?,?,?,?,?)",
        ("s_b", "sc1", "p1", "", "第二章 质量要求", 1, 1, "generated",
         "混凝土强度等级不低于C30，浇筑应振捣密实。"))
    await db_conn.commit()

    result = await sections_quality("sc1", db_conn)
    assert result["summary"]["checked"] == 2
    assert result["summary"]["problem_sections"] == 1
    assert result["items"][0]["section_id"] == "s_a"
    assert result["items"][0]["colloquial_hits"]

    # 空库：结构完整、计数为 0
    empty = await sections_quality("sc_none", db_conn)
    assert empty["summary"]["checked"] == 0
    assert empty["items"] == []


# ---------------------------------------------------------------------------
# 一致性管线与停止语义的整体行为（异步行为级，防止只在源码上自洽）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancelled_child_task_propagates_not_swallowed():
    """模拟「吞取消 vs re-raise」对调用方终态判断的影响。

    与 _run_consistency_pipeline 同构的最小模型：子协程被取消后——
    - 吞取消：await 子任务不抛，调用方误以为正常完成（BUG-1 的危害）；
    - re-raise：await 子任务抛 CancelledError，调用方进入停止分支。
    """
    async def pipeline(swallow: bool):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if swallow:
                return "done"
            raise
        return "done"

    # 吞取消 → 调用方拿不到任何异常（旧实现的缺陷形态）
    t1 = asyncio.create_task(pipeline(swallow=True))
    await asyncio.sleep(0.01)
    t1.cancel()
    r = await t1
    assert r == "done"  # 误判为正常完成

    # re-raise → 调用方正确感知取消（修复后形态）
    t2 = asyncio.create_task(pipeline(swallow=False))
    await asyncio.sleep(0.01)
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2


@pytest.mark.asyncio
async def test_bounded_queue_producer_blocked_not_lost():
    """有界队列背压语义：队列满时 await put 挂起、消费后投递成功（不丢不炸）。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    for i in range(2):
        await q.put(i)

    put_task = asyncio.create_task(q.put(3))
    await asyncio.sleep(0.01)
    assert not put_task.done()          # 满队列 → 生产者背压挂起
    assert await q.get() == 0
    await asyncio.wait_for(put_task, timeout=1)  # 消费后投递成功
    assert q.qsize() == 2