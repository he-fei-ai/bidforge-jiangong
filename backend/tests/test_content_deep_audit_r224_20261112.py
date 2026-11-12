"""正文生成模块深度审计增强 · R224 修复护栏（2026-11-12）。

覆盖本轮 24 项落地修复的反向锚（行为实证 + 源码锁），防回流。
判据均来自报告 docs/正文生成模块深度审计增强_R224_20261112.md：
  F1  current_ai_task_id 全仓零 .set() → 暂停跨任务误杀排队 AI 协程（P1）
  F2  _db_write_lock 每实例一把 → 跨方案并发任务在共享单连接上互不互斥（P1）
  F3  前端僵尸阈值 0.95 误判合法一致性阶段且抢先 return（P1）
  F4  consistency_repair_done 把 DB progress 写满 1.0 → 重挂提前合成 completed（P1）
  F5  _adj_sem 死旋钮（批内并发实为 10 非 3）（P2）
  F6  批调度缺 mode 闸（config 契约）+ auto_shrink_over=false 被批调度绕过（P2）
  F7  正文链无任务级配额熔断（对齐目录链 R213 FIX-11）（P2）
  F8  续写/压缩判据前无围栏补齐 → 每轮续写产出落进被吞区域、wc 恒不增长（P2）
  F9  update_section 审核失效判据在图表登记改写之前（P2）
  F10 编号修复审核退回绕过 review 单一写入口（P2）
  F11 update/shrink UPDATE 无 rowcount 守卫（P2）
  F12 ai_image 下方题注被删 + image 上方题注优先级倒挂（P2）
  F13 numbering repair/rollback 漏 invalidate_export_cache（P2）
  F14 手动压缩不传 max_tokens（违反压缩链明文设计约束）（P2）
  F15 收尾 schemes/review UPDATE 裸写无 retry/锁（P2）
  F16 暂停扣减缺口：_section_partial/running.elapsed_ms/EMA 校准三口径源（P2）
  F17 刷新接管 stopped 不 load + facts_result 两路不消费（P2）
  F18 空 taskId 守卫 fail-open（404 被当「已不存在」放行）（P3）
  F19 审计行缺失时 word_adjust 差值修正按 0 全额入账（P3）
  F20 _ckpt["charts"] 批调度二次落库后虚高（P3）
  F21 续写退避不分级（429 场景 1/3.3 间隔撞限流 provider）（P3）
  F22 重规范化日志打 word_status 代替 id + word_budget-only PATCH 响应恒 0 字（P3）
  F23 在线 completed 的 run_words/over_count 前端零消费（P3）
  F24 基线红（对拍表 3 常量）→ 并行会话 R220 已登记，复跑绿（无本轮代码）
"""

import asyncio
import importlib
import pathlib
import re

import pytest

# ⚠️ 初始化顺序契约（R218 教训③）：sse_handlers 必须先于 _sse_outline_build
# 初始化（后者顶部有 facade 延迟绑定 + 中段 from sse_handlers 转发导入）。
# 用 importlib.import_module 而非 import 语句 —— isort 会把 import 语句按
# 字母序重排（_sse_outline_build < sse_handlers），恰好破坏该顺序（实测踩坑）。
_sh = importlib.import_module("app.routers.sse_handlers")  # noqa: F401 — facade 先初始化
ob = importlib.import_module("app.routers._sse_outline_build")
cb = importlib.import_module("app.services.content_blocks")
_wfb = importlib.import_module("app.services.ai.workflows_base")
ResizableSemaphore = _wfb.ResizableSemaphore
current_ai_task_id = _wfb.current_ai_task_id
_cu = importlib.import_module("app.services.content_utils")
auto_fix_unclosed_fences = _cu.auto_fix_unclosed_fences
text_word_count = _cu.text_word_count

_BACK = pathlib.Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_BACK / rel).read_text(encoding="utf-8")


def _sse_src() -> str:
    return _src("app/routers/sse_handlers.py")


def _sections_src() -> str:
    return _src("app/routers/sections.py")


def _num_src() -> str:
    return _src("app/services/numbering.py")


def _conftest_src() -> str:
    return _src("tests/conftest.py")


# ---------------------------------------------------------------------------
# F1 · AI 并发闸按任务归属绑定（暂停不再跨任务误杀）
# ---------------------------------------------------------------------------
class TestF1AiTaskOwnerBinding:
    def test_reject_waiters_isolates_by_owner(self):
        """owner 绑定后，暂停 A 的 reject_waiters(owner_id=A) 不拒绝 B 的排队协程。"""

        async def main():
            sem = ResizableSemaphore(1)
            await sem.acquire()  # 占满容量，后续 acquire 全部排队
            token = current_ai_task_id.set("task-A")
            got: dict = {}

            async def _wa():
                got["a"] = await sem.acquire()

            async def _wb():
                current_ai_task_id.set("task-B")
                got["b"] = await sem.acquire()

            ta = asyncio.create_task(_wa())  # 继承 task-A 上下文
            tb = asyncio.create_task(_wb())
            for _ in range(4):
                await asyncio.sleep(0)
            rejected = sem.reject_waiters(owner_id="task-A")
            for _ in range(4):
                await asyncio.sleep(0)
            assert rejected == 1 and got.get("a") is False, "A 的排队协程未被拒绝"
            assert "b" not in got and not tb.done(), "B 的排队协程被跨任务误杀（owner 隔离失效）"
            ta.cancel()
            tb.cancel()
            current_ai_task_id.reset(token)
            await asyncio.gather(ta, tb, return_exceptions=True)

        asyncio.run(main())

    def test_sse_generators_bind_owner(self):
        """三条 SSE 生成链（content/outline/facts）必须在生成器内绑定 owner。"""
        src = _sse_src()
        assert src.count("current_ai_task_id.set(str(task_id))") >= 3, "owner 绑定点缺失（暂停跨任务误杀回流）"


# ---------------------------------------------------------------------------
# F2 · DB 写锁提升为模块级单锁
# ---------------------------------------------------------------------------
class TestF2GlobalDbWriteLock:
    def test_module_level_lock_shared(self):
        src = _sse_src()
        assert re.search(r"^_DB_WRITE_LOCK = asyncio\.Lock\(\)", src, re.M), "模块级写锁丢失"
        assert "_db_write_lock = asyncio.Lock()" not in src, "每实例锁回流（跨方案并发互斥失效）"
        assert src.count("_db_write_lock = _DB_WRITE_LOCK") == 1

    def test_cross_task_mutual_exclusion(self):
        """模块级单锁上两个并发临界区必须互斥（共享单连接的旁路提交防线）。"""
        from app.routers.sse_handlers import _DB_WRITE_LOCK

        order: list[str] = []

        async def worker(name: str):
            async with _DB_WRITE_LOCK:
                order.append(f"{name}-in")
                await asyncio.sleep(0.01)
                order.append(f"{name}-out")

        async def main():
            await asyncio.gather(worker("A"), worker("B"))

        asyncio.run(main())
        assert order in (
            ["A-in", "A-out", "B-in", "B-out"],
            ["B-in", "B-out", "A-in", "A-out"],
        ), f"临界区交叠：{order}"


# ---------------------------------------------------------------------------
# F4 · 运行期 DB progress 封顶 0.98（1.0 只属于终态）
# ---------------------------------------------------------------------------
def test_running_progress_never_writes_full_one():
    src = _sse_src()
    runs = re.findall(r"_update_progress_safe\(1\.0", src)
    assert len(runs) == 2, f"运行期写 1.0 的点应为 2（无章可生成 completed + 终态），实际 {len(runs)}"
    assert "_update_progress_safe(0.98, _m)" in src, "consistency_repair_done 封顶点丢失（重挂提前合成 completed 回流）"


# ---------------------------------------------------------------------------
# F5 · 批内并发闸生效
# ---------------------------------------------------------------------------
def test_word_adjust_semaphore_is_acquired():
    src = _sse_src()
    i = src.index("_adj_sem = asyncio.Semaphore(WORD_ADJUST_CONCURRENCY)")
    seg = src[i : i + 20000]
    assert "await _adj_sem.acquire()" in seg, "_adj_sem 死旋钮回流（批内并发实为批大小）"
    assert "_adj_sem.release()" in seg, "信号量释放缺失（批间串行会被永久占满）"


# ---------------------------------------------------------------------------
# F6 · 批调度 mode 闸 + auto_shrink_over 尊重
# ---------------------------------------------------------------------------
def test_batch_gate_has_mode_and_over_flags():
    src = _sse_src()
    i = src.index('bool(getattr(settings, "word_adjust_batch_enabled", False))')
    seg = src[i : i + 400]
    assert 'mode not in ("section", "continue")' in seg, "批调度 mode 闸缺失（单章模式二次续写/压缩回流）"
    assert "if not auto_shrink_over:" in src, "auto_shrink_over=False 的 over 过滤缺失"
    assert "classify_word_direction" in src, "over 过滤未复用 classify_word_direction 单一判据"


# ---------------------------------------------------------------------------
# F7 · 任务级配额熔断
# ---------------------------------------------------------------------------
def test_quota_breaker_roundtrip_and_wiring():
    from app.routers import sse_handlers as sh

    tid = "r224-quota-probe"
    assert not sh._is_quota_blocked(tid)
    sh._mark_quota_blocked(tid)
    assert sh._is_quota_blocked(tid)
    sh._QUOTA_BLOCKED_TASKS.discard(tid)
    assert not sh._is_quota_blocked(tid)

    src = _sse_src()
    assert src.count("_mark_quota_blocked(task_id)") == 2, "熔断标记必须恰好覆盖首稿+续写两处配额闸"
    assert "attempt == 0 and _is_quota_blocked(task_id)" in src, "章节重试循环顶的熔断跳过缺失"
    assert "_QUOTA_BLOCKED_TASKS.discard(str(task_id))" in src, "finally 清理缺失（标记跨任务泄漏）"
    assert '_m2._QUOTA_BLOCKED_TASKS.clear()' in _conftest_src(), "conftest 按用例隔离缺失"


# ---------------------------------------------------------------------------
# F8 · 未闭合围栏章跳过续写/压缩（产出必然落进被吞区域，白烧收口）
# ---------------------------------------------------------------------------
class TestF8UnclosedFenceSkip:
    def test_continuation_append_counts_after_autofix(self):
        """机理锚：落库补齐后续写式追加才可计数 —— 证明未闭合围栏下续写零收益。"""
        raw = "开头说明。\n```mermaid\ngraph TD\nA-->B\n"
        fixed = auto_fix_unclosed_fences(raw)[0]
        grown = fixed + "\n续写追加的正文需要被计入字数。" * 10
        assert text_word_count(grown) > text_word_count(raw), "补齐后续写追加不可计数（机理前提失效）"

    def test_judgement_sites_skip_on_unclosed_fence(self):
        """续写触发与压缩门槛两处判据都必须先探测未闭合围栏并跳过。"""
        src = _sse_src()
        i = src.index("R224（F8）：未闭合围栏章**跳过续写**")
        seg = src[i : i + 1300]
        assert "if find_unclosed_fences(result):" in seg, "续写侧未闭合围栏探测缺失"
        assert "await _continue_if_needed(" in seg, "else 分支续写调用丢失"
        assert "落库时补齐闭合" in seg
        assert "if auto_shrink_over and not find_unclosed_fences(result):" in src, "压缩侧未闭合围栏探测缺失"


# ---------------------------------------------------------------------------
# F9 · 审核失效判据在图表登记之后
# ---------------------------------------------------------------------------
def test_review_reset_after_chart_registration():
    src = _sections_src()
    i_chart = src.index("await register_inline_charts(")
    i_reset = src.index('if _old_content != (fields["content"] or ""):')
    assert i_chart < i_reset, "审核失效判据仍在图表登记之前（管线改写正文不退审核回流）"


# ---------------------------------------------------------------------------
# F10 · 编号修复审核退回走路由层单一写入口
# ---------------------------------------------------------------------------
def test_numbering_repair_review_reset_via_router():
    num = _num_src()
    # 剔注释行后再查（numbering 历史注释里提及过 from app.routers.export 的旧事）
    num_body = "\n".join(ln for ln in num.splitlines() if not ln.strip().startswith("#"))
    assert not re.search(r"from app\.routers", num_body), "services 层 import routers（依赖方向契约回流）"
    assert '"fixed_sections": _fixed_section_ids' in num, "修复章清单回报缺失"
    sec = _sections_src()
    i = sec.index("result = await repair_scheme_numbering_consistency(db, scheme_id)")
    seg = sec[i : i + 2500]
    assert 'reset_review_on_content_change(db, scheme_id, str(_sid), actor="编号一致性修复")' in seg, (
        "路由层未按 fixed_sections 走 review 单一写入口"
    )


# ---------------------------------------------------------------------------
# F11 · update/shrink UPDATE 的 rowcount 守卫
# ---------------------------------------------------------------------------
def test_update_and_shrink_updates_guarded():
    src = _sections_src()
    assert "safe_rowcount(_upd_cur" in src, "update_section 主 UPDATE 无 0 行守卫"
    assert "safe_rowcount(_shrink_cur" in src, "压缩落库 UPDATE 无 0 行守卫"


# ---------------------------------------------------------------------------
# F12 · 题注吸收白名单补 ai_image + 上方题注显式优先
# ---------------------------------------------------------------------------
class TestF12CaptionAbsorb:
    def _blocks_with_ai_image(self):
        return [
            {"type": "ai_image", "title": "", "code": '{"prompt": "现场照片"}', "inline": True},
            {"type": "paragraph", "text": "图 3-1 基坑支护现场照片"},
        ]

    def test_ai_image_below_caption_absorbed(self):
        out = cb._attach_trailing_figure_captions(self._blocks_with_ai_image())
        assert [b["type"] for b in out] == ["ai_image"], "ai_image 下方题注未被吸收（残留段落回流）"
        assert out[0].get("title") == "基坑支护现场照片", "题注未写入 ai_image.title"

    def test_image_above_caption_wins_over_alt(self):
        blocks = [
            {"type": "paragraph", "text": "图 2-1 基坑监测点布置图"},
            {"type": "image", "alt": "现场随手拍", "url": "http://x/1.png"},
        ]
        src = _src("app/services/content_blocks.py")
        i = src.index("_img_cap = _extract_figure_caption(blocks)")
        seg = src[i : i + 600]
        assert '"alt": _img_cap or (m_img.group("alt") or "").strip()' in seg, (
            "上方题注优先级倒挂回流（alt 非空时用户图题被静默丢弃）"
        )
        # 行为侧：下方通道语义（题注覆盖 alt）在上方案中保持
        _ = blocks  # 上方题注吸收发生在 _extract_figure_caption 调用点，源码锁已覆盖


# ---------------------------------------------------------------------------
# F13 / F14 · 导出缓存失效 + 手动压缩 max_tokens
# ---------------------------------------------------------------------------
def test_numbering_repair_and_rollback_invalidate_export_cache():
    src = _sections_src()
    for probe in ("编号一致性修复：导出缓存失效失败", "编号版本回滚：导出缓存失效失败"):
        i = src.index(probe)
        seg = src[max(0, i - 600) : i]
        assert "invalidate_export_cache(db, scheme_id)" in seg, f"{probe} 段缺失效调用（R46 家族回流）"


def test_manual_shrink_passes_max_tokens():
    src = _sections_src()
    i = src.index("手动压缩此前不传 max_tokens")
    seg = src[i : i + 900]
    assert "max_tokens=max_tokens_for_budget(word_budget)" in seg, "手动压缩无输出预算上界回流"


# ---------------------------------------------------------------------------
# F15 · 收尾写库持锁 + 重试
# ---------------------------------------------------------------------------
def test_finish_scheme_write_locked_and_retried():
    src = _sse_src()
    i = src.index("async def _finish_scheme_once")
    j = src.index("await retry_db_op(_finish_scheme_once", i)
    k = src.index("async with _db_write_lock:", i, j)
    assert i < k < j, "收尾 schemes 置位未持模块级写锁"
    assert "await retry_db_op(_mark_pending_once" in src, "审核置位 UPDATE 无重试（瞬态锁即任务 failed 回流）"


# ---------------------------------------------------------------------------
# F16 · 暂停扣减（渐近填充 / running.elapsed_ms / EMA 锚点）
# ---------------------------------------------------------------------------
class TestF16PauseDeduction:
    def test_section_partial_freezes_during_pause(self):
        rec = {"stage": "draft", "stage_started_at": 1000.0, "stage_started_paused_ms": 0.0}
        base = ob._section_partial(rec, 1100.0)
        frozen = ob._section_partial(rec, 1100.0, paused_ms=200_000.0)
        assert frozen < base, "暂停期间渐近填充仍在吃墙钟（心跳 progress 边暂停边走回流）"

    def test_anchor_deducts_only_post_anchor_pause(self):
        rec = {"stage": "draft", "stage_started_at": 1000.0, "stage_started_paused_ms": 150_000.0}
        v = ob._section_partial(rec, 1010.0, paused_ms=160_000.0)  # 锚后 10s 暂停
        v0 = ob._section_partial(rec, 1010.0, paused_ms=150_000.0)  # 无锚后暂停
        assert v < v0, "暂停扣减未按锚点水位取差（历史暂停段被重复扣除）"

    def test_running_elapsed_deducts_pause(self):
        import time as _t

        prog = {
            "running": {
                "s1": {"title": "t", "index": 1, "stage": "draft", "started_at": _t.monotonic() - 100.0}
            }
        }
        s0 = ob._snapshot_stats(prog, 0.0)["running"][0]["elapsed_ms"]
        s1 = ob._snapshot_stats(prog, 50_000.0)["running"][0]["elapsed_ms"]
        assert 40_000 <= s0 - s1 <= 60_000, "running[].elapsed_ms 未扣暂停（R215 B2 漏改的第三个口径源）"

    def test_stage_ema_anchor_wired(self):
        src = _sse_src()
        assert 'rec["stage_started_paused_ms"] = _paused_now' in src, "阶段切换的暂停水位锚点缺失"
        assert "_calibrate_stage_expect(_prog, prev_stage, _actual)" in src, "EMA 校准未用扣减后实测值"


# ---------------------------------------------------------------------------
# F19 / F20 / F21 · 批调度簿记三修正
# ---------------------------------------------------------------------------
def test_word_adjust_delta_fallbacks():
    src = _sse_src()
    assert "_old_wc = text_word_count(_content)" in src, "审计行缺失时按 0 全额入账回流（words 双计）"
    # charts 差值块必须与 `if count_progress:` 同级（门控之外），而非在其体内
    m = re.search(r"^ +(if count_progress:)", src, re.M)
    assert m, "count_progress 门控丢失"
    gate_indent = m.start(1) - (src.rfind("\n", 0, m.start(1)) + 1)  # 门控行首缩进（if 的列位置）
    k = src.index("_cur_charts = int((report or {})", m.start())
    t = src.rindex("try:", m.start(), k)  # 差值块自身的 try:（门控体内无 try）
    t_indent = t - (src.rfind("\n", 0, t) + 1)
    assert t_indent == gate_indent, (
        f"charts 差值块 try 缩进 {t_indent} 与门控 {gate_indent} 不符 —— 仍被 count_progress 门控"
    )
    assert "_chart_counts_by_sid" in src and "_prev_charts" in src, "per-section 差值修正缺失"


def test_continue_backoff_grades_rate_limit():
    src = _sse_src()
    i = src.index("R224（F21）：退避分级对齐首稿")
    seg = src[i : i + 400]
    assert "CONTENT_RATE_LIMIT_BACKOFF" in seg and "_is_rate_limit_text(str(cont_last_err" in seg, (
        "续写退避分级回流（429 场景以 1/3.3 间隔撞限流 provider）"
    )


# ---------------------------------------------------------------------------
# F22 · 日志定位与响应字数
# ---------------------------------------------------------------------------
def test_renorm_log_prints_section_id_and_response_wc():
    src = _sections_src()
    assert "str(upd[3])[:8]" in src, "重规范化失败日志仍打 word_status（排障无法定位章节）"
    i = src.index("_resp_wc = fields.get(\"word_count\")")
    seg = src[i : i + 400]
    assert "SELECT word_count FROM sections WHERE id=?" in seg, "缺字段时未读库内现值（0 字假提示回流）"


# ---------------------------------------------------------------------------
# F17 / F18 / F23 的后端侧锚（前端判据见 frontend contentDeepAuditR224.test.ts）
# ---------------------------------------------------------------------------
def test_checkpoint_payload_keys_still_produced():
    """F23 消费的前提：completed 载荷仍产出 run_words/over_count（防护栏反向锁死产出）。"""
    src = _sse_src()
    assert '"run_words": _run_words' in src
    assert '"over_count": _over_n' in src


@pytest.mark.parametrize(
    ("probe", "why"),
    [
        ('mode not in ("section", "continue")', "F6a mode 闸"),
        ("_sem_held = True", "F5 信号量持闸标记"),
        ("_finish_scheme_once", "F15 收尾重试闭包"),
        ("find_unclosed_fences(result)", "F8 未闭合围栏跳过"),
        ("_mark_quota_blocked", "F7 配额熔断"),
        ("_chart_counts_by_sid", "F20 配图差值"),
    ],
)
def test_probes_fail_when_renamed(probe: str, why: str):
    """自证：探针换名后必须失配（防恒真锁，R194 切片自证同款）。"""
    src = _sse_src()
    assert probe in src, f"{why} 探针失效（实现被改名而护栏未跟随）"
    alt = probe.replace("_", "X")
    if alt != probe:
        assert alt not in src
