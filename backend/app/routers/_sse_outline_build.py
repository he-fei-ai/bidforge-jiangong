"""目录生成辅助层（纯结构拆分自 sse_handlers.py）：目录骨架/校验/统计/ETA/进度/字数预算/章节抽取映射/单元抓取合并等构建期辅助函数。零行为变更：所有 facade 命名空间引用经 _sh 延迟绑定，保持 monkeypatch 语义不变。"""

import asyncio
import copy
import time

import app.routers.sse_handlers as _sh  # noqa: F401 — facade 命名空间延迟绑定（monkeypatch 兼容）


def _section_partial(rec: dict, now: float, expect_overrides: dict | None = None) -> float:
    """折算单个「进行中章节」在本章内已完成的进度比例（0~1）。

    = 阶段基准（base） + 阶段跨度（span）× 该阶段已耗时/预期耗时（封顶 85%）
    阶段基准保证跨阶段单调递增；时间渐近填充保证耗时最长的 draft 阶段
    进度条也在持续前进，而不是整段静止。

    expect_overrides：本次任务**在线校准后**的各阶段预期耗时（见
    `_calibrate_stage_expect`）；缺省时回退出厂经验值。

    ✅ B19（2026-09-23）：渐近填充必须按**阶段起点**（stage_started_at）
    计时，而不是整章起点（started_at）。旧实现用整章耗时：continue
    阶段在整章已跑 3 分钟后才开始，elapsed/expect 立刻 >1 → 该阶段开局
    就填满 85%，用户看到进度条"突然跳一格然后长时间不动"。
    旧调用方只传 started_at 时回退到它（向后兼容）。
    """
    stage = rec.get("stage", "")
    model = _sh._STAGE_MODEL.get(stage, _sh._STAGE_MODEL["draft"])
    base, span = model["base"], model["span"]
    expect = (expect_overrides or {}).get(stage) or model["expect"]
    try:
        # 校准值来自本任务的实测耗时（内部状态）：脏值/非数值一律回退出厂经验值，
        # 否则 elapsed/expect 会抛 TypeError 并把异常带进生成主流程。
        expect = float(expect)
    except (TypeError, ValueError):
        expect = model["expect"]
    if span <= 0 or expect <= 0:
        return base
    # ✅ 阶段起点优先；缺失时回退整章起点（兼容旧调用方）
    started = rec.get("stage_started_at")
    if started is None:
        started = rec.get("started_at", now)
    elapsed = max(0.0, now - started)
    frac = min(elapsed / expect, 1.0) * _sh._STAGE_FILL_MAX
    return base + span * frac


def _calibrate_stage_expect(prog: dict, stage: str, actual: float) -> None:
    """用实测阶段耗时 EMA 校准该阶段的预期耗时（供进度填充速率使用）。

    ✅ 为什么只在**阶段切换点**调用（调用方约定）：校准会改变 expect，而
    expect 变小会让「同一时刻算出的本章进度」变大、变大则相反 —— 若在阶段
    进行中反复调整，进度条可能回退。只在离开某阶段时写入新值，则该阶段内
    填充曲线始终连续；跨阶段的单调性由 base 累积（上一阶段完成值 = 下一阶段
    基准）保证，与校准无关。

    actual ≤ 0 或阶段已知预期 ≤ 0 时不做任何修改（保持安全默认）。
    """
    if not stage or actual <= 0:
        return
    overrides = prog.setdefault("stage_expect", {})
    base_expect = overrides.get(stage) or (_sh._STAGE_MODEL.get(stage, {}) or {}).get("expect") or 0.0
    if base_expect <= 0:
        return
    new_expect = (1 - _sh._STAGE_EXPECT_ALPHA) * base_expect + _sh._STAGE_EXPECT_ALPHA * actual
    overrides[stage] = round(min(max(new_expect, _sh._STAGE_EXPECT_MIN), _sh._STAGE_EXPECT_MAX), 2)


def _monotonic_progress(prev: float, new: float) -> float:
    """对外进度护栏：任何路径都不得让已上报的进度**回退**。

    背景：进度存在多个来源（章节阶段加权进度、准确性更细的统计折算、停止
    收尾、目录/正文共用口径）。任何一处口径调整或数据竞争都可能算出比上一条
    事件更小的值，进度条随即「倒退」，观感上等同于卡死或任务被重置。
    这里统一取「历史最大值」，把不可回退变成结构性约束。
    """
    try:
        new_v = float(new or 0.0)
    except (TypeError, ValueError):
        new_v = 0.0
    try:
        prev_v = float(prev or 0.0)
    except (TypeError, ValueError):
        prev_v = 0.0
    return round(max(new_v, prev_v, 0.0), 4)


def _weighted_progress(prog: dict) -> float:
    """把进度状态折算为 0~1 的平滑总进度。

    总进度 = _SECTION_PHASE_MAX × (已处理章节数 + Σ进行中章节的本章进度) / 总章数
    ——「已处理」含成功与失败（失败同样消耗了时间，否则进度永远到不了上限）。
    进行中章节按「阶段基准 + 阶段内按耗时渐近填充」贡献部分进度，使进度条在
    单章生成期间也持续前进，而不是等整章落库才跳一格。
    """
    total = prog.get("total") or 0
    if total <= 0:
        return 0.0
    processed = prog.get("done", 0)
    now = time.monotonic()
    overrides = prog.get("stage_expect") or None
    partial = sum(_sh._section_partial(rec, now, overrides) for rec in (prog.get("running") or {}).values())
    raw = (processed + partial) / total
    return round(min(raw, 1.0) * _sh._SECTION_PHASE_MAX, 4)


def _fmt_duration(ms) -> str:
    """把毫秒格式化为人类可读时长（用于进度消息文案）。"""
    if not ms or ms <= 0:
        return "0秒"
    sec = int(ms / 1000)
    if sec < 60:
        return f"{sec}秒"
    minutes, s = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes}分{s}秒" if s else f"{minutes}分钟"
    hours, m = divmod(minutes, 60)
    return f"{hours}小时{m}分" if m else f"{hours}小时"


def _done_counters(processed, failed) -> dict:
    """统一章节计数的单一口径（R175 BUG-4）。

    done       = 已**成功**落库的章节数；
    processed  = 已处理章节数（成功 + 失败，进度折算与 ETA 沿用此值）；
    failed     = 失败章节数。

    此前运行期进度事件 `done` 含失败、终态 checkpoint `done` 只含成功，
    同一任务在进度卡与终态消息/重挂摘要里会出现两个数字。现统一为：
    对外 `done` 恒为成功数，另以 `processed` 表达已处理总数。
    """

    def _int(v):
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    p = _int(processed)
    f = min(_int(failed), p)
    return {"done": p - f, "processed": p, "failed": f}


def _snapshot_stats(prog: dict, paused_ms: float = 0.0) -> dict:
    """构造运行统计快照（供 stats 事件与心跳通道使用，绝不抛异常）。

    字段口径：
      elapsed_ms  本次任务已耗时；eta_ms 线性外推的剩余耗时（无法预估时为 None）
      done/total  已成功章节数 / 总章节数；processed 已处理（含失败）；failed 失败数
      words       已落库正文累计字数
      avg_section_ms 已完成章节的平均实测耗时（无样本为 None）
      running     进行中章节列表（标题 + 当前阶段 + 该章已耗时）
      concurrency 当前并发档位
      phase       阶段标识（prepare/sections/consistency）+ 中文标签
      progress    折算后的总进度（与 progress 事件同口径，且经单调护栏）

    ✅ B1（2026-09-20）paused_ms：协作式暂停**不打断在飞 AI 调用**，
    暂停期间 stats 仍持续推送。若不扣除暂停时长，进度卡的「已耗时」会
    在用户"什么都没干"的暂停时间里持续增长，与活动中心
    （task_registry.get_task_elapsed_ms，已扣除暂停）口径相反，
    同一任务两个界面显示不同耗时。扣减后夹紧到 0（脏输入/超额暂停安全）。

    ✅ ETA 口径修复（2026-09-15 二次增强）：旧实现用「任务总耗时 / 已处理章数」
    外推，存在两处系统性偏差：
      1) 分母里含**准备阶段**（字数分配 AI 调用最长 180s）与全文一致性阶段；
         开头几章就会把 ETA 抬到与实际完全不符的量级；
      2) 忽略**进行中章节已投入的时间**：并发 3 章在跑而 done=0 时给出 None
         （或按陈旧样本高估），用户恰恰在最需要预估的开局看不到剩余时间。
    现改为「章节阶段有效进度」外推：
        有效进度 = 已处理章节数 + Σ进行中章节在本章内的完成比例（与进度同源）
        eta = 章节阶段已耗时 / 有效进度 × (总章数 − 有效进度)
    —— 与进度条同口径，ETA 归零与进度到 100% 同步，不再"100% 还要等很久"。
    """
    try:
        now = time.monotonic()
        # ✅ 扣除暂停时长，夹紧到 0（脏值/超额暂停都不得产出负耗时）
        try:
            _paused = max(0.0, float(paused_ms or 0))
        except (TypeError, ValueError):
            _paused = 0.0
        elapsed_ms = max(0, int((now - prog.get("started_at", now)) * 1000) - int(_paused))
        total = prog.get("total") or 0
        done = prog.get("done", 0)
        running = []
        for sid, rec in list((prog.get("running") or {}).items()):
            running.append(
                {
                    "section_id": sid,
                    "title": rec.get("title", ""),
                    "index": rec.get("index", 0),
                    "stage": rec.get("stage", ""),
                    "stage_label": rec.get("stage_label", ""),
                    "elapsed_ms": int((now - rec.get("started_at", now)) * 1000),
                }
            )
        # 平均单章耗时：已完成（含失败）章节的实测均值，用于「还要多久」判断
        # ✅ 单位修复：section_ms 列表实际存的是【秒】（见 _prog 初始化与逐章 append），
        #    对外字段名是 *_ms —— 必须换算毫秒，否则前端展示「平均 20 毫秒/章」。
        durations = prog.get("section_ms") or []
        avg_section_ms = int(sum(durations) / len(durations) * 1000) if durations else None
        # ETA：按「已处理 + 进行中部分贡献」的有效进度外推（口径见 docstring）
        eta_ms = None
        effective = done + sum(
            _sh._section_partial(rec, now, prog.get("stage_expect") or None)
            for rec in (prog.get("running") or {}).values()
        )
        if total > effective > 0:
            section_started = prog.get("sections_started_at")
            if section_started is None:
                # 兼容：调用方未记录章节阶段起点（老任务 / 单测）时退化为任务耗时。
                # elapsed_ms **已扣除暂停**，此处不得再减（否则二次扣减）。
                section_elapsed_ms = elapsed_ms
            else:
                # ✅ ETA 与 elapsed_ms 同口径：显式章节起点尚未扣暂停，需在此扣除
                #    （否则暂停后 ETA 会凭空多出暂停时长）。
                section_elapsed_ms = max(int((now - section_started) * 1000) - int(_paused), 0)
            # 至少按 1s 估算：首个统计点（耗时≈0）不应给出 0 或天量 ETA
            section_elapsed_ms = max(section_elapsed_ms, 1000)
            eta_ms = int(section_elapsed_ms / effective * (total - effective))
        counts = _done_counters(done, prog.get("failed", 0))
        return {
            "elapsed_ms": elapsed_ms,
            "eta_ms": eta_ms,
            "done": counts["done"],
            "processed": counts["processed"],
            "total": total,
            "failed": counts["failed"],
            "words": prog.get("words", 0),
            "avg_section_ms": avg_section_ms,
            "running": running,
            "concurrency": prog.get("concurrency", 0),
            "phase": prog.get("phase", ""),
            "phase_label": prog.get("phase_label", ""),
            # R175 BUG-3：心跳快照与 progress 事件共用单调护栏，
            # 防止 running 移除与 done 自增之间的窗口让进度条回退。
            "progress": _sh._monotonic_progress(prog.get("progress_max", 0.0), _sh._weighted_progress(prog)),
        }
    except Exception:
        # 统计只服务于展示，任何异常都不允许影响生成主流程
        _sh.logger.debug("正文生成运行统计快照构建失败（返回空，不影响生成）", exc_info=True)
        return {}


def _locked_progress_updater(lock: asyncio.Lock, task_id: str):
    """构造「在写锁内落库进度」的更新函数（工厂函数，便于单测锁语义）。

    ✅ BUG 修复（2026-09-16）：正文生成的进度落库（task_registry，自带 commit）
    与章节落库共用同一 aiosqlite 连接，而 `_persist_section` 的写入块是一个**跨多条
    语句的事务**（DELETE chart_predictions → INSERT … → UPDATE sections → commit）。
    此前进度 commit 不受请求内的 `_db_write_lock` 保护，并发章节下可能把
    **另一个章节的半途事务**顺带提交 —— 一旦该章节随后的 UPDATE/commit 失败，
    rollback 已无法回滚被提交的图表登记/删除变更，「事务整体原子」的守卫被旁路
    （表现为「正文里还有图表块、图表清单却没有登记」）。
    """

    async def _update(progress: float, message: str = "", *, event: str = "progress"):
        async with lock:
            await _sh.update_progress(task_id, progress, message, event=event)

    return _update


def _word_budget_hint(word_budget: int) -> str:
    """目标字数提示文案（提示词 / 续写消息共用，避免两处口径漂移）。

    与程序阈值的关系（不写死在文案里会来回漂移）：
      · 0.8X  → `WORD_UNDER_RATIO`：低于此值触发自动续写；
      · 1.3X  → `WORD_OVER_RATIO`：高于此值判定 word_status='over'（可压缩/自动压缩）；
      · 1.2X  → 对模型的**硬上限**（比 1.3X 留一档余量，避免模型"贴线"跑出 over）。
    实测（运行库 96 章）显示：不给区间与上限时，模型把目标当"下限"，
    平均写到 1.69X、最高 2.82X，72% 的章节被判 over —— 因此这里必须显式给出
    「可接受区间 + 硬上限 + 超写即不合格」三件事。
    """
    try:
        b = int(word_budget or 0)
    except (TypeError, ValueError):
        b = 0
    if b <= 0:
        return ""
    # ✅ R66（2026-10-11）：容差带由 settings.word_budget_tolerance 驱动。
    #    默认 0.1 时 1-0.1==0.9 / 1+0.1==1.1 / 1+2*0.1==1.2（IEEE754 逐位相等），
    #    文案与历史硬编码「0.9~1.1 倍 … 硬上限 1.2X」逐字节一致。
    try:
        tol = abs(float(getattr(_sh.settings, "word_budget_tolerance", 0.1)))
    except (TypeError, ValueError):
        tol = 0.1
    lo = 1.0 - tol
    hi = 1.0 + tol
    hard = 1.0 + 2.0 * tol
    return (
        f"{b}字（可接受 {lo:.1f}~{hi:.1f} 倍即 {int(b * lo)}~{int(b * hi)} 字；"
        f"硬上限 {int(b * hard)} 字，超出即视为不合格）"
    )


def _should_auto_shrink(content: str, word_budget: int, over_ratio: float = _sh.WORD_OVER_RATIO) -> bool:
    """是否需要对该章节跑「超字数自动压缩」（与 word_status='over' 同口径）。

    只看**正文字数**（剔除图表代码块，见 content_utils.text_word_count）——
    否则一张数百字符的 mermaid 图会把阈值顶过去，触发无意义的压缩调用。
    """
    try:
        wc = _sh.text_word_count(content or "")
        budget = int(word_budget or _sh.DEFAULT_WORD_BUDGET)
    except (TypeError, ValueError):
        return False
    if budget <= 0:
        return False
    return wc > budget * over_ratio


async def _await_with_stats(coro, push_stats, interval: float = _sh._STATS_PUSH_INTERVAL, task_id: str | None = None):
    """等待 AI 协程完成，期间每 interval 秒调用一次 push_stats 推送运行统计。

    背景：原实现在 await AI 调用期间完全不产出业务事件（只有 10s 一次的心跳
    comment，且前端不解析），进度条长时间静止。这里改为轮询式等待，周期性
    推送 stats（已耗时 / 进行中章节 / 累计字数 / ETA）。

    异常与取消语义与直接 await 完全一致：
      - coro 抛异常 → 原样重新抛出（调用方的重试 / 降级逻辑保持不变）；
      - 外层被取消 → 内部协程同步取消，不留悬挂任务。

    ✅ BUG 修复（2026-10-04）：停止语义不取消在飞 AI 调用。传 task_id 时把
       内层 task 注册进 task_registry 的 child_tasks，用户点停止 → 停止分支
       同步 `t.cancel()` 全部 child_tasks，本函数内层 await 立刻抛
       CancelledError → 目录生成链路能真正响应停止，不再等 collect_json_response
       的 180s 超时预算。与正文生成 L6296-6299 的 register_child_task 参考
       范式对齐（正文路径已按此接线，目录路径此前漏了）。
    """
    task = asyncio.ensure_future(coro)
    if task_id:
        # ✅ P2 修复（2026-10-04 · 子任务泄漏）：父任务已被 finish_task 清出
        #    _tasks 时，register_child_task 返回 False（旧实现静默跳过，返回
        #    None）。此时本函数创建的 task 不会被父任务的停止链路 cancel，
        #    会挂住直到 AI 超时预算耗尽 —— 用户点停止后依然占连接池。
        #
        # ⚠️ 采用"打 warning + 继续执行"而非直接 cancel：_ai_call_with_stop_awareness
        #    被多个调用点复用（目录生成 / 正文生成 / outline review 等），其中
        #    部分合法场景下 task_id 就是"父任务在跑但本 AI 调用不属于生成主链路"
        #    （例如 review 阶段以独立 task_id 触发）。硬 cancel 会误杀这些合法调用
        #    并让上游业务整体失败。真泄漏只在父任务已终态、AI 调用又是"本应属于
        #    父任务的收尾"时才成立，此时父任务侧早已发出停止信号或已 finished，
        #    AI 调用会随自然完成而结束，泄漏代价仅是"该 AI 调用的超时预算"，
        #    而非真正的资源挂死。日志足够让运维定位到这类异常链路。
        try:
            _reg_ok = _sh.register_child_task(task_id, task)
        except Exception:
            _reg_ok = False
        if not _reg_ok:
            _sh.logger.warning(
                "子任务登记失败：父任务 %s 已不存在（或登记异常），"
                "该 AI 调用将脱离父任务停止链路，仅靠自然完成/超时结束",
                task_id,
            )
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                return task.result()
            # ✅ BUG 修复（2026-10-04 · BUG-02）：push_stats 异常不得中断 AI 调用。
            # 旧实现 `await push_stats()` 无异常保护——一旦 push_stats 内部因
            # stats_provider 状态不一致 / DB 写入超时 / 客户端断线等抛错，异常
            # 会立刻冒泡到本函数，`finally` 分支随即 `task.cancel()` 掉在飞的
            # AI 任务，把已经跑了 30s~180s 的章节生成判为失败。这里降级为
            # warning 日志（stats 是"锦上添花"，AI 调用才是主线）。
            try:
                await push_stats()
            except asyncio.CancelledError:
                # 取消不能吞，否则外层 finally 已 cancel task 后循环仍会误跑。
                raise
            except Exception as e:
                _sh.logger.warning(
                    "SSE stats push failed (continuing AI call): %s",
                    e,
                    exc_info=True,
                )
    finally:
        if not task.done():
            task.cancel()


# ---------- 目录生成进度模型（2026-09-15 增强） ----------
# ✅ 背景：目录生成存在两段长时间「零业务事件」——
#    1) 每次目录 AI 调用（最长 OUTLINE_REQUEST_TIMEOUT = 180s）；
#    2) 审核 + 修复（最长 OUTLINE_REVIEW_TIMEOUT + OUTLINE_FIX_TIMEOUT = 180s）。
#    原实现只在阶段边界推一条 progress（0.1 → 0.6/0.3 → 0.9 → 1.0），
#    中间数分钟进度条完全静止，用户无法判断是「在跑」还是「卡死」。
# ✅ 现与正文生成同源处理：
#    - 阶段模型（base 累积基准 + span 跨度 + expect 预期耗时渐近填充）；
#    - 每次 AI 调用由 _await_with_stats 周期性推送 stats（3s 一次）；
#    - 心跳通道 stats_provider 兜底（审核/修复期间调用方正 await，无法 yield）。
_OUTLINE_PHASE_MODEL = {
    "prepare": {"base": 0.02, "span": 0.03, "expect": 4.0, "label": "准备中"},
    "draft": {"base": 0.08, "span": 0.47, "expect": 55.0, "label": "AI 生成目录"},
    "level1": {"base": 0.08, "span": 0.17, "expect": 40.0, "label": "生成一级目录"},
    "sublevels": {"base": 0.25, "span": 0.45, "expect": 0.0, "label": "生成二三级目录"},
    "review": {"base": 0.70, "span": 0.20, "expect": 45.0, "label": "目录审核中"},
    "fix": {"base": 0.90, "span": 0.10, "expect": 60.0, "label": "按审核意见修复"},
}

# ✅ 阶段耗时在线校准（2026-09-16 · 承接上一轮报告遗留建议 #1）：
#    上表的 expect 与 OUTLINE_CHAPTER_EXPECT 都是**出厂经验值**，与具体模型速度
#    差异很大（弱模型一级目录 150s、强模型 15s）。校准口径与正文生成的
#    _calibrate_stage_expect 完全一致：只用**本次任务实测耗时**做 EMA，且只在
#    阶段切换点写入（阶段内填充曲线保持连续，不会因 expect 变化而回退）。
_OUTLINE_EXPECT_MIN = 5.0  # 校准下限（秒）：防止个别极快样本把预期压到近 0
_OUTLINE_EXPECT_MAX = 600.0  # 校准上限（秒）
_OUTLINE_EXPECT_ALPHA = 0.3  # EMA 系数：新样本权重
# 校准样本下限（秒）：短于该值的阶段耗时不作为样本。
#   —— 同一阶段被重复 _set_phase 刷新（如 draft 的「已生成，正在构建目录树」）
#      会产生 0.x 秒的空转样本，若纳入 EMA 会把 expect 直接拽到下限，
#      下一阶段的填充速率随之失真。
_OUTLINE_CALIBRATE_MIN_SAMPLE = 5.0


def _outline_expect(st: dict, phase: str) -> float:
    """取某阶段的预期耗时（优先本任务校准值，回退出厂经验值；脏值安全）。

    只服务于「进度填充」与「阶段剩余时间」，任何异常都不得影响生成主流程，
    因此非数值/负数一律回退到出厂经验值。
    """
    model = _sh._OUTLINE_PHASE_MODEL.get(phase) or {}
    ov = st.get("stage_expect")
    if isinstance(ov, dict):
        try:
            val = float(ov.get(phase))
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    try:
        return float(model.get("expect") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _outline_chapter_expect(st: dict) -> float:
    """取「单章子目录」的预期耗时（优先本任务 EMA 校准值，回退出厂经验值）。"""
    try:
        val = float(st.get("chapter_expect") or 0.0)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return _sh.OUTLINE_CHAPTER_EXPECT


def _calibrate_outline_expect(st: dict, phase: str, actual: float) -> None:
    """用实测阶段耗时 EMA 校准该阶段的预期耗时。

    ✅ 调用约定：**只在离开某阶段时**调用（见 _advance_outline_phase）。
    理由与正文生成 _calibrate_stage_expect 相同：expect 变小会让「同一时刻算出的
    阶段进度」变大、变大则相反 —— 阶段进行中反复调整会让进度条回退；只在切换点
    写入则阶段内曲线连续，跨阶段单调性由 base 累积（上一阶段完成值 = 下一阶段基准）
    保证，与校准值无关。

    actual 小于 _OUTLINE_CALIBRATE_MIN_SAMPLE 时视为无效样本（空转阶段），忽略。
    """
    if not phase or actual < _sh._OUTLINE_CALIBRATE_MIN_SAMPLE:
        return
    base_expect = _sh._outline_expect(st, phase)
    if base_expect <= 0:
        return
    overrides = st.get("stage_expect")
    if not isinstance(overrides, dict):
        overrides = {}
        st["stage_expect"] = overrides
    new_expect = (1 - _sh._OUTLINE_EXPECT_ALPHA) * base_expect + _sh._OUTLINE_EXPECT_ALPHA * actual
    overrides[phase] = round(min(max(new_expect, _sh._OUTLINE_EXPECT_MIN), _sh._OUTLINE_EXPECT_MAX), 2)


def _calibrate_outline_chapter_expect(st: dict, actual: float) -> None:
    """用实测单章耗时 EMA 校准「单章预期耗时」（决定 sublevels 阶段的填充速率）。"""
    if actual < _sh._OUTLINE_CALIBRATE_MIN_SAMPLE:
        return
    base = _sh._outline_chapter_expect(st)
    if base <= 0:
        return
    new = (1 - _sh._OUTLINE_EXPECT_ALPHA) * base + _sh._OUTLINE_EXPECT_ALPHA * actual
    st["chapter_expect"] = round(min(max(new, _sh._OUTLINE_EXPECT_MIN), _sh._OUTLINE_EXPECT_MAX), 2)


def _advance_outline_phase(st: dict, phase: str) -> None:
    """切换目录生成阶段（统一入口）：先按实测耗时校准**上一阶段**，再写入新阶段。

    ✅ 单一来源：_set_phase 与审核/修复子阶段回调共用本函数，避免两处各写一遍
    导致「校准点漂移」（漏校准 → 填充速率永远是出厂值；重复校准 → 样本被稀释）。

    同一阶段重复刷新时**保留 phase_started_at**：否则该阶段的耗时样本会被截断成
    0.x 秒（被 _OUTLINE_CALIBRATE_MIN_SAMPLE 丢弃，真实耗时样本永久丢失）。
    """
    now = time.monotonic()
    prev = st.get("phase", "")
    if prev != phase:
        _sh._calibrate_outline_expect(st, prev, now - st.get("phase_started_at", now))
        st["phase_started_at"] = now
    st["phase"] = phase
    st["chapter_started_at"] = now


def _outline_push_value(st: dict) -> float:
    """计算本次 progress 事件的进度值（**不可回退护栏**），并写回 last_p。

    ✅ BUG 修复（2026-09-16）：分步链路原实现 `p = _outline_progress(_prog)` 后
    直接把 p 塞进 progress 事件，只有 last_p 取 max —— 于是 `_set_phase("sublevels",
    force=0.3)` 之后的第一次「本章完成」事件（模型折算值 0.25 + 0.45×(1/10) ≈ 0.295）
    会让前端进度条从 30% **倒退**到 29%。这里统一走 _monotonic_progress 护栏，
    对外进度结构性不可回退（与正文生成同口径）。
    """
    p = _sh._monotonic_progress(st.get("last_p", 0.0), _sh._outline_progress(st))
    st["last_p"] = p
    return p


def _outline_progress(st: dict) -> float:
    """折算目录生成的平滑总进度（0~1）。

    - 分步子目录阶段（sublevels）有确定的章节总数，按「已完成章数 + 当前章
      按已耗时渐近填充」推进，比时间外推更准；
    - 其余阶段用 base + span × min(已耗时/预期耗时, 1) × 85% 填充。

    base 为累积基准（上一阶段完成值），保证阶段切换时进度不倒退。
    """
    phase = st.get("phase", "prepare")
    model = _sh._OUTLINE_PHASE_MODEL.get(phase) or _sh._OUTLINE_PHASE_MODEL["prepare"]
    base, span = model["base"], model["span"]
    now = time.monotonic()
    if phase == "sublevels":
        total = st.get("sub_total") or 0
        done = st.get("sub_done", 0)
        if total <= 0:
            frac = 0.0
        else:
            cur = 0.0
            if done < total:
                elapsed = max(0.0, now - st.get("chapter_started_at", now))
                # ✅ 单章预期耗时走本任务 EMA 校准值（弱模型下 45s 的出厂值会让
                #    填充迅速顶格后长时间「静止」）
                chapter_expect = _sh._outline_chapter_expect(st)
                if chapter_expect > 0:
                    cur = min(elapsed / chapter_expect, 1.0) * _sh._STAGE_FILL_MAX
            frac = min((done + cur) / total, 1.0)
    else:
        # ✅ 阶段预期耗时走本任务 EMA 校准值（阶段切换时写入，阶段内取值恒定
        #    ⇒ 填充曲线连续、单调）
        try:
            expect = float(_sh._outline_expect(st, phase))
        except (TypeError, ValueError):
            expect = 0.0
        if span > 0 and expect > 0:
            elapsed = max(0.0, now - st.get("phase_started_at", now))
            frac = min(elapsed / expect, 1.0) * _sh._STAGE_FILL_MAX
        else:
            frac = 0.0
    return round(min(base + span * frac, 1.0), 4)


def _outline_phase_label(st: dict) -> str:
    """当前阶段的用户可读标签（progress 事件与 stats 快照共用，单一来源）。"""
    return (_sh._OUTLINE_PHASE_MODEL.get(st.get("phase", "")) or {}).get("label", "")


def _outline_eta_ms(st: dict, now: float, progress: float) -> int | None:
    """目录生成的「预计剩余」外推（口径与正文生成 _snapshot_stats 同源）。

    ✅ BUG 修复（2026-09-16）：旧实现有三个系统性缺陷：
      1) **一次性直出链路（短方案）永远没有 ETA** —— 分支条件写成 `if total > done`
         而该链路 total=0，`0 > 0` 为假，"按阶段预期耗时外推" 的分支**不可达**，
         用户在唯一需要预估的开局看到的是空白；
      2) **分步链路分母含准备阶段与一级目录** —— 用 `任务总耗时 / 已完成章数`
         外推，准备阶段（事实/知识库构建 + 一级目录 AI 最长 180s）被摊到每章上，
         ETA 被系统性放大（实测 2 章/60s 却给出 40 分钟）；
      3) **审核/修复阶段（章节已全部完成）返回 None** —— 恰恰在最长 180s 的
         审核+修复区间里，"预计剩余"消失。
    现改为两段式口径：
      ① 有已完成章节样本时：按「子目录阶段」有效进度外推 ——
         有效进度 = 已完成章数 + 当前章按已耗时折算的贡献（与进度条同源），
         分母只用 sub_started_at 起的耗时，不含准备/一级目录；
      ② 无样本（首章前 / 一次性直出 / 审核修复）：按当前阶段**校准后**的预期
         耗时给出阶段剩余时间。
    进度已达 100% 时统一返回 None（无剩余），避免"100% 还要等很久"的观感。
    """
    if progress >= 1.0:
        return None
    total = st.get("sub_total") or 0
    done = st.get("sub_done", 0) or 0
    if total > done and done > 0:
        chapter_expect = _sh._outline_chapter_expect(st)
        cur = 0.0
        if chapter_expect > 0:
            elapsed_cur = max(0.0, now - st.get("chapter_started_at", now))
            cur = min(elapsed_cur / chapter_expect, 1.0) * _sh._STAGE_FILL_MAX
        effective = done + cur
        if effective <= 0:
            return None
        # ✅ 分母优先取「子目录阶段」起点（进入 sublevels 时写入）。
        #    缺失（旧任务/单测）时退回任务耗时，保持与历史口径兼容。
        base_at = st.get("sub_started_at") or st.get("started_at", now)
        span_ms = max(int((now - base_at) * 1000), 1000)  # ≥1s：首个统计点不给出 0
        return int(span_ms / effective * max(total - effective, 0.0))
    expect = _sh._outline_expect(st, st.get("phase", ""))
    if expect > 0:
        left = expect - max(0.0, now - st.get("phase_started_at", now))
        if left > 0:
            return int(left * 1000)
    return None


def _snapshot_outline_stats(st: dict) -> dict:
    """目录生成的运行统计快照（字段与正文 _snapshot_stats 同构，绝不抛异常）。

    done/total 在分步链路中表示「已生成子目录的章数 / 总章数」；
    一次性直出链路 total=0（无章节维度），前端据此隐藏章节计数。

    progress 取「阶段模型折算值」与「已推送最大值（last_p）」的较大者 ——
    单步链路里两者可能不一致（如 _set_phase 用 force 对齐可见刻度），
    对外只暴露一个口径，避免进度条/统计块互相矛盾。
    """
    try:
        now = time.monotonic()
        elapsed_ms = int((now - st.get("started_at", now)) * 1000)
        total = st.get("sub_total") or 0
        done = st.get("sub_done", 0)
        progress = _sh._monotonic_progress(st.get("last_p", 0.0), _sh._outline_progress(st))
        snap = {
            "elapsed_ms": elapsed_ms,
            "eta_ms": _sh._outline_eta_ms(st, now, progress),
            "done": done,
            "total": total,
            "failed": len(st.get("failed_chapters") or []),
            "words": 0,
            "concurrency": 0,
            "running": [],
            "phase": st.get("phase", ""),
            "phase_label": _sh._outline_phase_label(st),
            "progress": progress,
            "stepwise": bool(st.get("stepwise")),
            "nodes": st.get("nodes", 0),
        }
        # ✅ R181（2026-11-06）：空章定向重试状态（仅在发生重试时挂键，默认路径零新增）
        _retry = st.get("chapter_retry") or {}
        if _retry:
            snap["chapter_retry"] = {
                "attempted": len(_retry),
                "recovered": sum(1 for v in _retry.values() if (v or {}).get("status") == "recovered"),
                "exhausted": sum(1 for v in _retry.values() if (v or {}).get("status") == "exhausted"),
                "chapters": [
                    {
                        "chapter": k,
                        "attempts": int((v or {}).get("attempts") or 0),
                        "status": (v or {}).get("status", ""),
                    }
                    for k, v in sorted(_retry.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)
                ],
            }
        return snap
    except Exception:
        # 统计只服务于展示，任何异常都不允许影响生成主流程
        _sh.logger.debug("目录生成运行统计快照构建失败（返回空，不影响生成）", exc_info=True)
        return {}


def _rank_sections_by_basis(text: str, basis) -> tuple[str, int]:
    """把解析提取结果的 Markdown 小节按「与方案名称的相关性」**前置**（不删除）。

    同上：``format_downstream_context`` 按 ``ANALYSIS_ITEMS`` 权威顺序输出（与
    本方案无关的项照样占位），而 ``_budgeted_truncate_sections`` 会按比例截断
    正文 —— 尾部项可能只剩标题。相关性前置后，方案名称涉及的项在固定预算下
    **全部可见**，这正是要求一「与方案名称相关的解析提取内容必须进入目录」。
    """
    if not text or basis is None or not getattr(_sh.settings, "outline_basis_relevance", False):
        return text, 0
    try:
        from app.services.scheme_basis import rank_by_relevance

        sections = _sh._split_md_sections(text)
        ordered, hit = rank_by_relevance(sections, basis, text_fn=lambda s: f"{s[0]} {s[1]}")
        if not hit:
            return text, 0
        return "\n".join(f"{t}\n{b}" if t else b for t, b in ordered), hit
    except Exception:  # pragma: no cover - 纯优化，失败回退原序
        _sh.logger.warning("按方案名称相关性前置提取小节失败（保持原序）", exc_info=True)
        return text, 0


def _split_md_sections(text: str) -> list[tuple[str, str]]:
    """把 Markdown 文本按 **二级标题**（`## `）切成小节。

    返回 [(小节标题, 小节正文)]；首个 `## ` 之前的内容归入 ("", 前言)。

    只认 `## `：**`### ` 及更深层级不切** —— 它们是二级小节内部的
    子结构，切开会打散"一节=一个提取项"的对应关系，导致同一提取项
    被当成多项、上下文断裂。
    """
    if not text:
        return [("", "")]
    lines = text.split("\n")
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        if line.startswith("## ") and not line.startswith("### "):
            sections.append((line.strip(), []))
        else:
            sections[-1][1].append(line)
    return [(title, "\n".join(body)) for title, body in sections]


def _budgeted_truncate_sections(text: str, budget: int) -> tuple[str, dict]:
    """按小节比例截断长文本（保底 + 封顶），返回 (截断后文本, 统计报告)。

    统计报告 {"sections": 小节数, "truncated_sections": 被截断的小节数}。

    稳定性契约：**每个小节的标题都保留**（只截正文）—— 目录生成需要看到
    "有哪 20 个提取项"，即便每项只剩开头一句；否则尾部项会整段消失。
    """
    sections = _sh._split_md_sections(text)
    lengths = [len(body) for _title, body in sections]
    allocs = _sh._allocate_char_budgets(lengths, budget)
    parts: list[str] = []
    truncated = 0
    for (title, body), alloc in zip(sections, allocs):
        if len(body) <= alloc:
            parts.append(f"{title}\n{body}" if title else body)
            continue
        truncated += 1
        if alloc <= 0:
            # 无配额：只留标题，保证该项"存在"这件事对模型可见
            parts.append(title)
            continue
        parts.append(f"{title}\n{body[:alloc]}" if title else body[:alloc])
    out = "\n".join(parts)
    return out, {"sections": len(sections), "truncated_sections": truncated}


async def _load_knowledge_rows(
    db, scheme_id: str, degraded_out: list | None = None, degraded_stage: str = "outline.assemble.knowledge_text"
) -> list[dict]:
    """加载方案可用的知识库条目行（专属 + 项目共享），一次查询供逐章内存过滤。

    ✅ D-39（R176）：查询异常此前只落 WARNING 日志，用户看到的是「没有知识库
    素材」而不是「素材读取失败」。失败时登记 `context_degraded`（segment_failed），
    正常（含确实无素材）路径不产生任何记录。
    """
    try:
        cur = await db.execute(_sh._fe.SCHEME_PROJECT_ID_SQL, (scheme_id,))
        row = await cur.fetchone()
        project_id = row["project_id"] if row else ""
        if not project_id:
            return []
        cur = await db.execute(
            "SELECT name, usage_hint, content FROM knowledge_base "
            "WHERE project_id=? AND (scheme_id=? OR scheme_id='')",
            (project_id, scheme_id),
        )
        return [dict(r) for r in await cur.fetchall()]
    except Exception as e:  # 表缺失等异常不应阻断生成
        _sh.logger.warning("加载项目知识库素材失败（降级为无）: %s", e)
        _sh._record_context_degraded(
            degraded_out,
            field="knowledge_rows",
            stage=degraded_stage,
            original_chars=0,
            kept_chars=0,
            reason=_sh.CONTEXT_DEGRADED_SEGMENT,
            segment_id="knowledge_rows",
        )
        return []


def _filter_knowledge_rows(rows: list[dict], leaf: dict) -> list[dict]:
    """按章节标题/描述对知识条目做相关性预筛（与事实逐章精选同规则）。

    无关键词或命中为空时回退全量，绝不因预筛丢失素材。
    """
    if not rows:
        return rows
    title = f"{(leaf.get('title') or '')} {(leaf.get('description') or '')}"
    kws = _sh._facts_keywords(title)
    if not kws:
        return rows
    filtered = [
        r
        for r in rows
        if any(kw in f"{r.get('name','')} {r.get('usage_hint','')} " f"{r.get('content','')}" for kw in kws)
    ]
    return filtered or rows


async def _build_knowledge_text(
    db,
    scheme_id: str,
    relevant_to: dict | None = None,
    degraded_out: list | None = None,
    degraded_stage: str = "outline.assemble.knowledge_text",
) -> str:
    """构建「项目知识库素材」文本（knowledge_base 表，产品需求 §3.9）。

    查询范围：该方案的专属条目（scheme_id=当前方案）+ 项目共享条目（scheme_id=''）。
    目录生成 / 正文生成共用；查询异常降级为空字符串，绝不阻断生成。

    ✅ 逐章精选（relevant_to）：与全局事实同一套 2-gram 关键字预筛规则，
    按章节标题/描述过滤相关素材，命中为空回退全量（长方案下避免把整本
    企业制度库无差别注入每一章，浪费 token 并稀释指令）。
    """
    from app.routers.knowledge import build_knowledge_text

    rows = await _sh._load_knowledge_rows(db, scheme_id, degraded_out=degraded_out, degraded_stage=degraded_stage)
    if relevant_to is not None:
        rows = _sh._filter_knowledge_rows(rows, relevant_to)
    _kn_stats: dict = {}
    text = build_knowledge_text(rows, stats_out=_kn_stats)
    # ✅ D-39（R176）：单条 500 / 总量 3000 的硬切进统一降级簿记。
    _sh._record_knowledge_budget(degraded_out, _kn_stats, stage=degraded_stage, field="knowledge_text")
    return text


# ---------- 辅助：递归构建 parent_chain ----------


def _build_parent_chain(
    all_sections: list[dict],
    section_id: str,
    nodes: dict[str, dict] | None = None,
    children_by_parent: dict[str, list[dict]] | None = None,
) -> str:
    """从叶子向上递归拿到 '第N章 > N.M > ...' 的父级章节标题链。

    优化：支持传入预构建的 nodes 和 children_by_parent，避免每次调用重建（O(N²) → O(N)）。
    编号原则（与 outline_utils.py 一致）：一级用"第X章"，二级用 "N"，三级用 "N.M"。
    """
    if nodes is None:
        nodes = {s["id"]: s for s in all_sections}
    if children_by_parent is None:
        children_by_parent = {}
        for s in all_sections:
            pid = s.get("parent_id", "")
            children_by_parent.setdefault(pid, []).append(s)
        for pid in children_by_parent:
            children_by_parent[pid].sort(key=lambda x: x.get("sort_order", 0))

    def _get_chapter_index(node: dict) -> int:
        """获取节点在其同胞中的 1-based 索引"""
        parent_id = node.get("parent_id", "")
        siblings = children_by_parent.get(parent_id, [])
        for idx, sib in enumerate(siblings):
            if sib["id"] == node["id"]:
                return idx + 1
        return 1

    parts: list[str] = []
    cur = nodes.get(section_id)
    parent_id = cur.get("parent_id") if cur else None
    while parent_id and parent_id in nodes:
        p = nodes[parent_id]
        title = p.get("title", "")
        # ✅ 编号统一（2026-09-25）：一律由**存储态编号**折算**展示态编号**，
        # 与 numbering.stored_id_to_display / 导出 heading_v2 同源。
        # 旧实现一级用「同胞索引 → 第X章」、二级以下直接吐 outline_json.id
        # （存储态），于是同一个祖先在正文提示词里显示 "1.1 第一节"，
        # 而导出版是 "1 第一节" —— 同一份文档两套编号，模型据此写出的
        # 子标题编号（3.2.1）也会与成稿（2.1）错位一整级。
        stored = _sh.stored_outline_id(p)
        if stored:
            level_tag = _sh.stored_id_to_display(stored)
        else:
            # 历史脏数据（无 outline_json / id 非法）：按同胞位置折算，
            # 保证链里既不出现 UUID 也不出现空标签（" 标题"）。
            idx = _get_chapter_index(p)
            level_tag = f"第{_sh._cn_number(idx)}章" if int(p.get("level") or 1) == 1 else str(idx)
        parts.append(f"{level_tag} {title}".strip())
        parent_id = p.get("parent_id")
    parts.reverse()
    return " > ".join(parts) if parts else "（顶级章节）"


def _apply_word_budget_allocations(
    units: dict[str, dict],
    override: int,
    ai_allocations: dict[str, int],
) -> None:
    """将字数预算写入各预算单元 units[unit_id]["alloc"]。

    规则：
    - 单叶子单元已在调用方直接分配，此处跳过；
    - 多叶子单元优先采用 AI 返回的 allocations（按叶子 id 键），
      若总和恰好等于 override 则采用，否则降级为均分（基础均分 + 余数摊给前若干个，
      保证总和守恒）。

    ✅ 修复：必须写回 units 字典（含 leaves），而不要写回临时的 units_payload
    （仅含 children、无 leaves），否则后续 apply 阶段读取 u["alloc"] 会 KeyError，
    导致整个生成任务崩溃。
    """
    for u in units.values():
        unit_leaves = u.get("leaves", [])
        if len(unit_leaves) <= 1:
            continue
        assigned = {lf["id"]: ai_allocations.get(lf["id"]) for lf in unit_leaves}
        if all(isinstance(v, int) and v > 0 for v in assigned.values()) and sum(assigned.values()) == override:
            u["alloc"] = assigned
        else:
            base = override // len(unit_leaves)
            rem = override - base * len(unit_leaves)
            u["alloc"] = {lf["id"]: base + (1 if i < rem else 0) for i, lf in enumerate(unit_leaves)}
            _sh.logger.info("单元 %s 字数分配降级均分（每份 %d）", u["unit"].get("title", "")[:20], base)


def _section_outline_number(sec: dict) -> str:
    """取章节目录编号（如 1.1.1），用于提示词中的『当前章节编号』。

    ✅ 编号统一（2026-09-25）：收敛到 services/numbering.stored_outline_id
    （唯一事实源）。旧实现在 outline_json 缺失/非法时回退到主键 id，
    而主键是 UUID —— 于是「没有待生成章节编号」的方案，其提示词里会出现
    一串 `550e8400-e29b-41d4-a716-446655440000`。模型会照抄进正文子标题，
    或据此误判层级（UUID 含连字符、不含点分段），成稿编号彻底失真。
    现一律返回合法点分路径，非法即空串（由提示词侧「编号缺失」兜底口径接管）。
    """
    return _sh.stored_outline_id(sec)


#: 章节编号缺失时的提示词兜底文案。
#: ✅ 不得渲染成空串：空串会让模型把「当前章节编号」当成"没有编号"，
#:    转而自行编造一个（实测出现过 1./1.1 与实际位置完全不符的子标题）。
#:    显式说明"未提供、不得编造"是唯一能约束模型的做法。
SECTION_NUMBER_MISSING = "（未提供，请勿自行编造章节编号）"

#: 目录二级小节常见简称 → 九大章节 key。
#: 目录里「3.2 施工部署」「4.1 施工方法」这类小节标题与一级章标准名
#: 既不相等也无互含关系（标准名是「施工计划」「施工工艺技术」），
#: 缺这张别名表会让最常见的小节完全拿不到结构化提取内容。
#: 只收录**语义唯一**的简称；有多义的一律不列（宁可不注入也不误归）。
CHAPTER_TITLE_ALIASES: dict[str, str] = {
    "施工部署": "plan",
    "部署与进度": "plan",
    "进度计划": "plan",
    "施工进度": "plan",
    "劳动力配置": "plan",
    "施工方法": "technique",
    "施工工艺": "technique",
    "工艺技术": "technique",
    "施工做法": "technique",
    "安全措施": "safety",
    "安全保障": "safety",
    "安全技术": "safety",
    "人员配备": "personnel",
    "管理人员": "personnel",
    "安全员": "personnel",
    "质量验收": "acceptance",
    "验收标准": "acceptance",
    "应急预案": "emergency",
    "应急处置": "emergency",
    "应急救援": "emergency",
    "计算书": "calc_drawings",
    "图纸": "calc_drawings",
    "监测方案": "technique",
    "监测措施": "technique",
}


def _section_number_for_prompt(sec: dict) -> str:
    """提示词用的『当前章节编号』——编号缺失时返回**显式兜底文案**。

    与 _section_outline_number 的区别：后者供**程序内部**判断（空串即可），
    本函数供**提示词渲染**，必须给出可读的兜底说明。

    ✅ 双重防护（回归锁定）：
    1. UUID 主键绝不作为编号注入（_section_outline_number 已拦截，
       本函数再兜一层，防止未来有人改回「回退主键」的老写法）；
    2. 缺失/非法一律返回含「未提供」的文案，且**非空** —— 空串会诱导
       模型自行编造章号，成稿编号与目录树整体错位。
    """
    return _sh._section_outline_number(sec) or _sh.SECTION_NUMBER_MISSING


def _continue_failed_flag(continue_failed: bool, word_count, word_budget) -> bool:
    """续写失败且字数仍不达标 → 置位 continue_failed（BUG-4，2026-09-19）。

    背景：续写轮失败 / 产出被丢弃时**只记日志**，前端无从区分
    「本章字数达标」与「本章欠字数且补写失败」—— 两者 word_status 都是 under，
    用户看不出为什么这章明显偏短，也无法判断要不要手工补。

    规则（与 content_utils.WORD_UNDER_RATIO 同口径）：
      · 未发生续写失败 → 永不为 True（不打扰够不着写不够的用户）；
      · 续写失败且字数**仍低于**预算的 80% → True；
      · 续写失败但后续轮补够了（≥ 预算×80%）→ False（已自愈，不报问题）。

    脏值防御：预算/字数为 None、0、"abc" 等一律不抛异常；预算无效（≤0）
    时保守判 False —— 无法判定达标线时不应给用户挂一个无法解释的红标。
    """
    if not continue_failed:
        return False
    try:
        wb = int(word_budget or 0)
    except (TypeError, ValueError):
        wb = 0
    # 预算无效（≤0 / 非数值）→ 无法判定达标线，保守判 False：
    # 不能给用户挂一个无法解释的红标。
    if wb <= 0:
        return False
    try:
        wc = int(word_count or 0)
    except (TypeError, ValueError):
        # 字数不可解析（None / "abc" / dict）→ 视为 0。
        # 方向要「偏真」：续写确实失败且拿不到有效字数，就是"没写够"，
        # 此时报 continue_failed 才能提示用户去手工补。
        wc = 0
    return wc < int(wb * _sh.WORD_UNDER_RATIO)


def chapter_key_of_title(title: str) -> str:
    """章节标题 → 九大章节 key（空串 = 未分类，**绝不猜**）。

    用于「结构化提取按章注入」：目录里的一级章标题（「3.2 施工部署」）
    要映射到九大章节之一，才能取到对应的提取项。

    匹配策略：
    1. 先剥掉标题自带的编号前缀（"1 " / "3.2 " / "第一章 " 等）——
       目录标题普遍带号，不剥则精确匹配全落空；
    2. 剥后与 NINE_CHAPTERS 标题**完全相等** → 直接命中；
    3. 否则按「标题互含」**最长优先**匹配（"施工安全保证措施" 含
       "安全保证措施"）。最长优先避免短标题抢先命中长标题
       （"计算书及相关图纸" 不应被 "图纸" 之类的短词抢走）。

    未命中一律返回空串：宁可漏注入（退回全局事实注入）也不错误归类 ——
    错归会把事实挤到别的章节的提示词里，比不注入危害更大。
    """
    if not title:
        return ""
    from app.services.facts_classification import TITLE_TO_CHAPTER

    bare = _sh.strip_outline_numbering(str(title)).strip()
    if not bare:
        return ""
    if bare in TITLE_TO_CHAPTER:
        return TITLE_TO_CHAPTER[bare]
    # 3. 互含匹配（最长优先）："施工安全保证措施" 含 "安全保证措施"
    best_key, best_len = "", 0
    for ch_title, key in TITLE_TO_CHAPTER.items():
        if ch_title and (ch_title in bare or bare in ch_title):
            if len(ch_title) > best_len:
                best_key, best_len = key, len(ch_title)
    if best_key:
        return best_key
    # 4. 二级小节别名（"施工部署" / "施工方法" …）：与一级章标准名既不
    #    相等也无互含关系，缺这一步会让最常见的目录小节拿不到提取内容
    for alias, key in _sh.CHAPTER_TITLE_ALIASES.items():
        if alias in bare:
            return key
    return ""


async def _build_chapter_extraction_map(
    db, project_id: str, sections: list[dict], nodes: dict, children_by_parent: dict
) -> dict[str, str]:
    """按九大章节把「结构化提取」内容映射到**叶子章节**（正文按章注入）。

    返回 {section_id: 注入文本}，**只含叶子章节**（有子节点的不注入）。

    映射链路：叶子 → 向上找**顶层祖先**（level=1）→ 用章节标题经
    facts_classification.classify_chapter_from_text 判出九大章节 key →
    取该章 source_items 对应的提取项内容拼装（映射表唯一事实源：
    scheme_classification.NINE_CHAPTERS）。

    设计取舍：
    - 只按**顶层**章节归属，不为每个二/三级节单独找提取项：目录里
      「1 工程概况 / 1.1 现状 / 1.2 特点」共享同一批项目基本信息，
      逐节细分既无数据支撑，也会让同一段文本在提示词里重复多次；
    - 顶层章节自身（level=1 且无子节点）不注入 —— 它们是「预算单元」
      而非正文最小单位，注入会让单元提示词冗长；
    - 无提取结果 / 未分类 → 返回 {}，调用方退回原有全局事实注入。
    """
    if not isinstance(sections, list) or not sections:
        return {}
    # ① 取该项目的成功提取项（与 _build_structured_brief 同源口径）
    try:
        cur = await db.execute(
            "SELECT item_id, label, output_type, content FROM bid_analysis_items "
            "WHERE project_id=? AND status='success' AND content IS NOT NULL "
            "AND content != ''",
            (project_id,),
        )
        rows = await cur.fetchall()
    except Exception:
        _sh.logger.warning("章节提取映射：读取结构化提取失败（退回原行为）", exc_info=True)
        return {}
    if not rows:
        return {}
    from app.services.bid_analysis_service import is_missing_result

    items: dict[str, str] = {}
    for r in rows:
        content = r["content"] or ""
        if is_missing_result(content, r["output_type"] or "markdown"):
            continue
        label = str(r["label"] or r["item_id"] or "").strip()
        items[str(r["item_id"])] = f"【{label}】\n{content}" if label else content
    if not items:
        return {}

    # ② 九大章节 key → source_items（唯一事实源：scheme_classification）
    from app.services import scheme_classification as _sc

    chapter_items: dict[str, list[str]] = {}
    for ch in getattr(_sc, "NINE_CHAPTERS", []) or []:
        key = str(ch.get("key") or "")
        if key:
            chapter_items[key] = list(ch.get("source_items") or [])

    def _has_children(sec: dict) -> bool:
        return bool(children_by_parent.get(sec.get("id", ""), []) or [])

    def _top_ancestor(sec: dict) -> dict:
        """向上找顶层（level=1）祖先；走到顶仍无父节点时返回当前节点。"""
        cur_node = sec
        for _ in range(20):  # 深度上限：脏数据成环时不至于死循环
            pid = cur_node.get("parent_id") or ""
            parent = nodes.get(pid) if pid else None
            if not parent:
                break
            cur_node = parent
        return cur_node

    out: dict[str, str] = {}
    for sec in sections:
        if not isinstance(sec, dict) or _has_children(sec):
            continue
        top = _top_ancestor(sec)
        if top.get("id") == sec.get("id") and int(top.get("level") or 1) == 1:
            continue  # 顶层章节自身即叶子 → 不注入（见 docstring）
        title = str(top.get("title") or "")
        if not title:
            continue
        key = _sh.chapter_key_of_title(title)
        if not key:
            continue
        blocks = [items[i] for i in chapter_items.get(key, []) if i in items]
        if blocks:
            out[sec["id"]] = "\n\n".join(blocks)
    return out


# ---------- 目录审核（短方案/长方案共用） ----------
def _validate_outline(obj, *, strict_depth: bool = True, max_nodes: int = 500) -> list[str]:
    """校验 AI 返回的 outline JSON 结构。

    strict_depth=True（默认，供审核/单测使用）：层级超过 3 级会作为"问题"上报，
    便于把这类目录标记出来；
    strict_depth=False（目录生成主链路使用）：忽略"层级过深"告警。
    max_nodes：节点总数上限；<=0 表示不限制。生成链路用默认 500 防止模型
    无限输出，审核修复链路用 OUTLINE_FIX_MAX_NODES 放宽（见 _outline_fix_validate_fn）。

    ✅ BUG 修复：旧实现把"层级 > 3"一律当作致命问题，而这些调用点用
    collect_json_response(messages, _validate_outline) 做校验——模型只要返回
    四级目录，就会连续触发两轮 JSON 修复、最终 raise ValueError，
    导致整次目录生成失败，永远走不到 clamp_outline_depth 的三级兜底裁剪。
    现在生成链路改用 strict_depth=False，深度超限交由裁剪处理。
    """
    issues = []
    ol = obj.get("outline", [])
    if not isinstance(ol, list) or not ol:
        issues.append("outline 为空或格式错误")
        return issues

    def _check(nodes, depth=0):
        if depth > 10:
            # 循环/异常深度的保护性截断；仅严格模式上报
            if strict_depth:
                issues.append(f"目录层级过深（>{10}层），可能存在循环引用")
            return
        if depth > 3 and strict_depth:
            # 超过系统裁剪上限（3层）时提前告警，
            # 用户可知道 AI 返回的深层节点会被裁剪并入 description
            issues.append(f"L{depth+1} 节点超出系统支持的3级目录上限，将被裁剪并入父节点描述")
        for node in nodes:
            if not isinstance(node, dict):
                issues.append(f"L{depth+1} 节点不是字典对象")
                continue
            title = node.get("title")
            if not title or not str(title).strip():
                issues.append(f"L{depth+1} 节点缺少 title 或 title 为空")
            # ✅ BUG-O4 修复：区分"缺失 children 字段"和"children=None"。
            # 旧实现把缺失字段也判为错误，但缺失 children 是合法叶节点（AI 常省略）。
            # 只有显式 children=None 或 children 非列表时才报错。
            if "children" in node:
                children = node.get("children")
                if children is None:
                    issues.append(f"L{depth+1} 节点 {node.get('title','?')} 的 children 为 null")
                elif not isinstance(children, list):
                    issues.append(f"L{depth+1} 节点 {node.get('title','?')} children 不是列表")
                elif children:
                    _check(children, depth + 1)

    _check(ol)
    if max_nodes > 0 and _sh._count_nodes(ol) > max_nodes:
        issues.append(f"节点总数超过上限 {max_nodes}")
    return issues


def _count_nodes(nodes: list) -> int:
    """递归计算 outline 节点总数（带深度保护）"""
    count = 0

    def _count(ns, depth=0):
        nonlocal count
        if depth > 12:
            return
        for n in ns:
            count += 1
            children = n.get("children") if isinstance(n, dict) else None
            if isinstance(children, list) and children:
                _count(children, depth + 1)

    _count(nodes)
    return count


def _outline_validate_fn(obj) -> list[str]:
    """目录生成主链路使用的校验函数：只拦截结构性问题，层级超限交由裁剪兜底。

    见 _validate_outline 的 strict_depth 说明。节点上限走
    ``OUTLINE_GENERATE_MAX_NODES``（P2-2 配置化，默认 500 = 原行为）。
    """
    return _sh._validate_outline(obj, strict_depth=False, max_nodes=_sh.OUTLINE_GENERATE_MAX_NODES)


def _outline_fix_validate_fn(obj) -> list[str]:
    """审核修复结果的校验函数：在生成链路校验基础上放宽节点数上限。

    ✅ BUG 修复：修复轮输入/输出都是**完整目录**（长方案 30 章 × 20+ 节点
    极易超过 500）。沿用生成链路的 500 上限时，修复结果会被一律判为非法 →
    走 "修复后校验仍不通过，保留原目录" 分支，"按审核建议自动修复" 整轮空转，
    用户看到的仍是未修正的目录。这里改用 OUTLINE_FIX_MAX_NODES。
    """
    return _sh._validate_outline(obj, strict_depth=False, max_nodes=_sh.OUTLINE_FIX_MAX_NODES)


def _sublevel_validate_fn(o) -> list[str]:
    """分步链路「单章子目录」的校验函数。

    必须是非空节点数组，且每个节点都是含 title 的 dict。
    （弱校验 lambda 会放过 ["x", 1] 这类畸形数组，normalize 静默丢弃后
    章变空壳且不进 failed_chapters，用户看到一堆只有章标题的空章节。）

    ✅ 从 generate_outline 内的闭包提升到模块级：原实现定义在事件流闭包内，
    无法单测；该函数是「章变空壳」这一线上问题的主要拦截点。

    ✅ BUG 修复（2026-09-29 · title 为 JSON null 时漏判）：旧实现写
    `str(n.get("title", ""))` —— 该写法只处理「键缺失」，而弱模型常返回
    **键存在但值为 null**（JSON null → Python None）；`str(None)` 得 `"None"`，
    `.strip()` 后仍为真值，于是 `{"title": null, "description": "..."}` 被
    误判为合法标题节点放行。随后在 _merge_unit_results 里对同一 None 调
    `.strip()` 直接抛 `AttributeError: 'NoneType' object has no attribute 'strip'`，
    整次目录生成以「目录生成失败」告终（用户白跑数分钟）。现统一用
    `str(x or "")` 同时覆盖「键缺失」与「值为 null」。
    """
    ol = o.get("outline")
    if not isinstance(ol, list) or not ol:
        return ["outline 必须为非空数组"]
    bad = [n for n in ol if not isinstance(n, dict) or not str(n.get("title") or "").strip()]
    return [f"存在 {len(bad)} 个非法/缺标题节点"] if bad else []


def _level1_validate_fn(o) -> list[str]:
    """长方案分步链路「一级目录」的校验函数。

    ✅ BUG 修复（2026-09-16）：旧实现用内联 lambda
    `[] if o.get("outline") else ["缺少 outline"]` —— 只看"非空"，
    `{"outline": ["工程概况", 1, null]}` 这类**非空但节点不是对象**的结果会被放行；
    随后生成循环里的 `ch.get("title")`（以及 `enumerate(level1)` 后的
    `ch["title"] = ...`）会抛 AttributeError，整次目录生成以"目录生成失败"告终，
    用户只能重试且看不到真实原因（弱模型返回字符串数组是常见形态）。
    这里要求：非空数组 + 每项都是对象。

    注意**不**要求 title 非空：生成循环对缺标题的节点会兜底为「第 N 章」
    （宽容优先，避免因为一个标题缺失就让整次生成失败）。
    """
    ol = o.get("outline")
    if not isinstance(ol, list) or not ol:
        return ["outline 必须为非空数组"]
    bad = [n for n in ol if not isinstance(n, dict)]
    return [f"存在 {len(bad)} 个非法节点（应为对象）"] if bad else []


def _sublevel_batch_validate_fn(o) -> list[str]:
    """长方案分步「多章合并」批量子目录响应的校验函数。

    契约（宽松但非空壳放行）：chapters 必须是数组；每项必须是含
    chapter_id 的对象，且其 outline 必须是**数组**（允许为空数组 ——
    某些章确实没有子目录，硬判非空会触发无意义的 JSON 修复轮）。

    注意：缺章**不在此处判失败** —— 批内缺章由 _fetch_unit_children 逐章
    回退单章调用兜底（"绝不整批判死"），若在此处报错则整批重试，
    反而放大调用次数。
    """
    chs = o.get("chapters")
    if not isinstance(chs, list) or not chs:
        return ["chapters 必须为非空数组"]
    for c in chs:
        if not isinstance(c, dict) or not str(c.get("chapter_id") or "").strip():
            return ["chapters 中存在缺 chapter_id 的非法项"]
        if not isinstance(c.get("outline"), list):
            return [f"chapter {c.get('chapter_id')} 的 outline 必须是数组"]
    return []


def _outline_patch_validate_fn(o) -> list[str]:
    """外科式补齐（patch）响应的校验函数。

    只要求 new_chapters 是**非空数组**，每项含非空 title。
    不在此处校验 children 结构：patch 只需产出缺失章的一级+二三级骨架，
    结构完整性由合并后的 normalize_outline 统一裁剪/重排兜底。
    """
    ncs = o.get("new_chapters")
    if not isinstance(ncs, list) or not ncs:
        return ["new_chapters 必须为非空数组"]
    bad = [c for c in ncs if not isinstance(c, dict) or not str(c.get("title") or "").strip()]
    return [f"存在 {len(bad)} 个缺标题的非法章节"] if bad else []


def _outline_skeleton(nodes: list, max_nodes: int = _sh.OUTLINE_REVIEW_MAX_NODES) -> list:
    """生成精简目录骨架（用于审核提示词）。

    ✅ 修复：旧实现直接 json.dumps(outline, ensure_ascii=False)[:3000] 做字符截断，
    会把 JSON 截成非法结构（括号/引号不闭合、字段残缺），审核模型据此容易误判
    "目录不完整"，进而触发无意义的修复轮次。改为按节点数预算递归裁剪，
    输出始终是合法 JSON，且保留完整层级轮廓供审核判断章节覆盖度。

    ✅ 增强：携带 description（截断 60 字）——审核要点包含"本章要写什么是否贴合"
    "是否存在重复、遗漏"，仅凭标题无法判断节点意图；description 体量可控
    （150 节点 × 60 字 ≈ 9K 字符），显著提升审核信息量。

    ✅ 遗留修复（2026-09-16）：总节点数超预算时，旧实现按「前序优先」截断，
    超长目录（>150 节点）的尾部章节完全不会进入审核视野 → 审核对尾章的
    重复/遗漏/错位零检出能力。现改为按一级章均衡取样：每个一级章标题保留
    1 个节点预算，剩余预算均分给各章子树，保证每章都有代表节点被审核。
    """
    if not isinstance(nodes, list) or not nodes:
        return []
    budget = [max_nodes]

    def _walk(ns, depth: int = 0) -> list:
        out: list = []
        if depth > 10 or budget[0] <= 0:
            return out
        for n in ns:
            if budget[0] <= 0:
                break
            if not isinstance(n, dict):
                continue
            budget[0] -= 1
            item: dict = {"title": n.get("title", "")}
            desc = str(n.get("description", "")).strip()
            if desc:
                item["description"] = desc[:60]
            children = n.get("children")
            if isinstance(children, list) and children:
                kids = _walk(children, depth + 1)
                if kids:
                    item["children"] = kids
            out.append(item)
        return out

    if _sh._count_nodes(nodes) <= max_nodes:
        return _walk(nodes, 0)

    # 超预算：按一级章均衡取样
    chapters = [n for n in nodes if isinstance(n, dict)]
    if not chapters or len(chapters) >= max_nodes:
        # 一级章本身多到连标题都放不下（极端场景）：退化为前序优先，
        # 优先保住尽可能多的章标题（章覆盖比章内细节对审核更关键）
        return _walk(nodes, 0)

    # 章标题各占 1，剩余预算均分给各章子树（每章子树至少 1 个节点）
    per_chapter = max(1, (max_nodes - len(chapters)) // len(chapters))
    out: list = []
    for ch in chapters:
        if budget[0] <= 0:
            break
        budget[0] -= 1  # 章标题
        item: dict = {"title": ch.get("title", "")}
        desc = str(ch.get("description", "")).strip()
        if desc:
            item["description"] = desc[:60]
        children = ch.get("children")
        if isinstance(children, list) and children:
            saved = budget[0]
            sub_budget = min(per_chapter, budget[0])
            budget[0] = sub_budget
            kids = _walk(children, 1)
            # 未用完的子树预算归还总池，让后面的章多分一点
            budget[0] = saved - (sub_budget - budget[0])
            if kids:
                item["children"] = kids
        out.append(item)
    return out


def _build_partial_preview(full_outline: list) -> list:
    """构造长方案分步生成的「目录预览」载荷（深拷贝 + 全树规范化）。

    ✅ 契约（2026-09-25 · E2 修复）：预览的 (id, title, description) 必须与
    收尾 normalize_outline 的产物**逐节点完全一致**。旧实现只对 `preview[-1]`
    （当前章）调 clamp_outline_depth，已完成章的深层节点原样下发：
      - 分步生成过程中前端预览出现 4/5 级节点（收尾时又消失），
        用户看到目录树「生成完又变了一次"」；
      - 深层标题未并入父节点 description，内容线索在预览里"看起来丢了"。

    实现上直接复用 normalize_outline（clamp + renumber 的唯一实现），
    避免"预览一套、收尾另一套"的规则漂移；深拷贝保证不污染主数据
    （clamp 是就地修改语义，浅拷贝会共享子节点字典）。
    """
    if not isinstance(full_outline, list) or not full_outline:
        return []
    preview = copy.deepcopy(full_outline)
    _sh.normalize_outline(preview)
    return preview


# ✅ R38 D4 收口（2026-10-03）：project_facts 提示词注入预算的**单一事实源**。
#   此前 1500/1000 以字面量散落五处调用点，口径漂移靠专项复盘才发现（D4）。
#   数值维持原样（零行为变化），仅消灭魔法数并显式化「谁不截断、为什么」：
#   · OUTLINE 档 = 外科式补齐 / 目录反馈修正 / 长方案一级目录（与旧值 1500 同）
#   · SUBLEVEL 档 = 二三级小节生成（提示词压力更大，预算更紧，与旧值 1000 同）
#   · 短方案主目录（outline_short_system）与审核（outline_review_system）
#     **有意不截断** —— 审核是只读核对、事实越全判得越准；调用点有注释锁定。
# ✅ 2026-10-06（R47 债-1）：常量值搬入 ``services/ai/prompts/_limits.py`` 单一事实源，
#    本模块仍以同名可读，下游消费代码零改动。
from app.services.ai.prompts._limits import (  # noqa: E402,F401
    PROJECT_FACTS_LIMIT_OUTLINE,
    PROJECT_FACTS_LIMIT_SUBLEVEL,
)


def _join_tail_budget(items: list[str], max_chars: int) -> str:
    """把若干条目标签用 `; ` 连接，**超预算时优先保留最近（靠后）的条目**。

    背景：二三级目录生成的提示词要注入「已生成的前序章节小节」以抑制跨章重复。
    旧实现 `"; ".join(items)[:1500]` 有两个问题：
      1) 按字符硬截断 → 最后一条常被截成半截标题，模型据此判重时信息失真；
      2) 保留的是**最早**的章节 —— 而越靠后的章节与当前章越相关（刚生成的
         内容更容易被重复），预算不够时应优先保留最近的部分。
    """
    if not items:
        return ""
    budget = max(int(max_chars), 0)
    picked: list[str] = []
    used = 0
    for item in reversed(items):  # 从最近往回取
        text = str(item).strip()
        if not text:
            continue
        cost = len(text) + (2 if picked else 0)  # 分隔符 "; "
        if budget and used + cost > budget:
            break
        picked.append(text)
        used += cost
    picked.reverse()
    return "; ".join(picked)


#: 来源 → 截断台账里的展示名（与 input_coverage.SRC_* 同源）
_TRUNC_LABELS = {"解析提取": "项目资料摘要", "全局事实": "全局事实"}


def _site_trunc_extra(*args) -> dict:
    """登记装配点的**实际**截断，返回 {来源: 截断说明}。

    签名：`_site_trunc_extra(source…, text, budget[, text, budget, …])`
    —— **前导项是来源名**（可多个），其后是「文本, 预算」交替的变长参数，
    第 i 组对应第 i 个来源。

    实参示例（一个装配点有两路输入被截）：
        _site_trunc_extra("解析提取", "全局事实",
                          text_a, 2000, text_b, 1500)
        → {"解析提取": "项目资料摘要 3500 字超本装配点预算 2000 字，已截断",
            "全局事实": "全局事实 1600 字超本装配点预算 1500 字，已截断"}

    M3（2026-09-23）：台账必须按各装配点的**真实预算**入账，不能统一用
    全局上限 —— 否则「本装配点本可以放得下」也会被记成截断，运维排查时
    被大量假告警淹没。未超预算的项**不记录**（零记录 = 未截断）。
    """
    from app.services import input_coverage as ic

    known = {ic.SRC_PARSE, ic.SRC_FACTS, ic.SRC_SCOPE, ic.SRC_REQ, ic.SRC_BASIS, ic.SRC_STANDARDS}
    args = list(args)
    sources = [a for a in args if isinstance(a, str) and a in known]
    rest = [a for a in args if not (isinstance(a, str) and a in known)]
    extra: dict = {}
    for j, k in enumerate(range(0, len(rest) - 1, 2)):
        text, budget = rest[k], rest[k + 1]
        try:
            budget_i = int(budget or 0)
            size = len(text or "")
        except (TypeError, ValueError):
            continue
        if budget_i <= 0 or size <= budget_i:
            continue
        src = sources[j] if j < len(sources) else (sources[-1] if sources else "")
        if not src:
            continue
        label = _sh._TRUNC_LABELS.get(src, src)
        extra[src] = f"{label} {size} 字超本装配点预算 {budget_i} 字，已截断"
    return extra
