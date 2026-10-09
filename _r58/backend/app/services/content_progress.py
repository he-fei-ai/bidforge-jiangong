"""正文生成 · 进度模型（从 sse_handlers.py 抽离，2026-09-21）

纯函数 + 常量，供正文生成 / 目录生成共用。抽离动机：sse_handlers.py
单文件 7000 行，进度模型是本文件内最自包含的一段（不依赖 router 上下文），
先抽到服务层，后续再按「章节生成器 / 终态收尾」继续拆。
"""
from __future__ import annotations

import time

# ---------- 正文生成进度模型（2026-09-15 增强） ----------
# 现改为三段式：
#    1) 章节内阶段事件 section_stage（context → draft → continue → persist）；
#    2) 按阶段权重折算的平滑总进度（进行中章节也贡献部分进度）；
#    3) 心跳通道周期性推送 stats（已耗时 / 进行中章节 / 累计字数 / ETA）。
SECTION_PHASE_MAX = 0.95
# 章节内阶段进度模型：base 进入该阶段时已完成的累积比例（保证单调递增），
# span 该阶段自身占本章的比例（合计 1.0），expect 该阶段预期耗时（秒）。
STAGE_MODEL = {
    "context":  {"base": 0.00, "span": 0.05, "expect": 3.0},
    "draft":    {"base": 0.05, "span": 0.55, "expect": 90.0},
    "continue": {"base": 0.60, "span": 0.30, "expect": 60.0},
    "persist":  {"base": 0.90, "span": 0.10, "expect": 12.0},
}
# 阶段内渐近填充上限：只填到 span 的 85%，给「阶段完成」留出可见的跳变
STAGE_FILL_MAX = 0.85
STAGE_LABELS = {
    "context": "构建上下文",
    "draft": "AI 生成中",
    "continue": "续写扩充中",
    "persist": "清洗与落库",
}
# AI 长调用期间推送运行统计的间隔（秒）
STATS_PUSH_INTERVAL = 3.0
# 阶段耗时 EMA 校准参数（只在阶段切换点更新，见 calibrate_stage_expect）
STAGE_EXPECT_MIN = 5.0      # 校准下限（秒）
STAGE_EXPECT_MAX = 600.0    # 校准上限（秒）
STAGE_EXPECT_ALPHA = 0.3    # EMA 系数

def section_partial(rec: dict, now: float,
                    expect_overrides: dict | None = None) -> float:
    """折算单个「进行中章节」在本章内已完成的进度比例（0~1）。

    = 阶段基准（base） + 阶段跨度（span）× 该阶段已耗时/预期耗时（封顶 85%）
    阶段基准保证跨阶段单调递增；时间渐近填充保证耗时最长的 draft 阶段
    进度条也在持续前进，而不是整段静止。
    """
    stage = rec.get("stage", "")
    model = STAGE_MODEL.get(stage, STAGE_MODEL["draft"])
    base, span = model["base"], model["span"]
    expect = (expect_overrides or {}).get(stage) or model["expect"]
    try:
        # 脏值/非数值一律回退出厂经验值，避免 TypeError 带进生成主流程
        expect = float(expect)
    except (TypeError, ValueError):
        expect = model["expect"]
    if span <= 0 or expect <= 0:
        return base
    elapsed = max(0.0, now - rec.get("started_at", now))
    frac = min(elapsed / expect, 1.0) * STAGE_FILL_MAX
    return base + span * frac


def calibrate_stage_expect(prog: dict, stage: str, actual: float) -> None:
    """用实测阶段耗时 EMA 校准该阶段的预期耗时（供进度填充速率使用）。

    只在阶段切换点调用（调用方约定）：校准会改变 expect，在阶段进行中反复
    调整会让进度回退。只在离开某阶段时写入新值，则阶段内填充曲线连续；
    跨阶段单调性由 base 累积保证。
    """
    if not stage or actual <= 0:
        return
    overrides = prog.setdefault("stage_expect", {})
    base_expect = overrides.get(stage) or (STAGE_MODEL.get(stage, {}) or {}).get("expect") or 0.0
    if base_expect <= 0:
        return
    new_expect = (1 - STAGE_EXPECT_ALPHA) * base_expect + STAGE_EXPECT_ALPHA * actual
    overrides[stage] = round(min(max(new_expect, STAGE_EXPECT_MIN),
                                 STAGE_EXPECT_MAX), 2)


def monotonic_progress(prev: float, new: float) -> float:
    """对外进度护栏：任何路径都不得让已上报的进度回退（取历史最大值）。"""
    try:
        new_v = float(new or 0.0)
    except (TypeError, ValueError):
        new_v = 0.0
    try:
        prev_v = float(prev or 0.0)
    except (TypeError, ValueError):
        prev_v = 0.0
    return round(max(new_v, prev_v, 0.0), 4)


def weighted_progress(prog: dict) -> float:
    """把进度状态折算为 0~1 的平滑总进度。

    总进度 = SECTION_PHASE_MAX × (已处理章节数 + Σ进行中章节的本章进度) / 总章数
    ——「已处理」含成功与失败（失败同样消耗了时间，否则进度永远到不了上限）。
    """
    total = prog.get("total") or 0
    if total <= 0:
        return 0.0
    processed = prog.get("done", 0)
    now = time.monotonic()
    overrides = prog.get("stage_expect") or None
    partial = sum(section_partial(rec, now, overrides)
                  for rec in (prog.get("running") or {}).values())
    raw = (processed + partial) / total
    return round(min(raw, 1.0) * SECTION_PHASE_MAX, 4)


def fmt_duration(ms) -> str:
    """把毫秒格式化为人类可读时长（用于进度消息文案）。"""
    if not ms or ms <= 0:
        return "0秒"
    sec = int(ms / 1000)
    if sec < 60:
        return f"{sec}秒"

def snapshot_stats(prog: dict, paused_ms: float = 0.0) -> dict:
    """构造运行统计快照（供 stats 事件与心跳通道使用，绝不抛异常）。

    字段：elapsed_ms / eta_ms / done / total / failed / words /
    avg_section_ms / concurrency / running / phase / phase_label / progress

    ETA 按「章节阶段有效进度」外推（分母不含准备/一致性阶段），
    与进度条同口径，ETA 归零与进度到 100% 同步。
    暂停扣除：elapsed_ms 扣掉暂停时长，与活动中心口径对齐（B1）。
    """
    try:
        now = time.monotonic()
        elapsed_ms = max(0, int((now - prog.get("started_at", now)) * 1000 - paused_ms))
        total = prog.get("total") or 0
        done = prog.get("done", 0)
        failed = prog.get("failed", 0)
        words = prog.get("words", 0)
        phase = prog.get("phase", "")
        phase_label = prog.get("phase_label", phase)
        concurrency = prog.get("concurrency", 0)
        progress = monotonic_progress(prog.get("progress_max", 0.0),
                                      weighted_progress(prog))
        running = []
        for sid, rec in (prog.get("running") or {}).items():
            running.append({
                "section_id": sid,
                "title": rec.get("title", ""),
                "index": rec.get("index", 0),
                "stage": rec.get("stage", ""),
                "stage_label": rec.get("stage_label", rec.get("stage", "")),
                "elapsed_ms": int(max(0, now - rec.get("started_at", now)) * 1000),
            })
        avg_section_ms = None
        section_ms = prog.get("section_ms") or []
        if section_ms:
            avg_section_ms = int(sum(section_ms) / len(section_ms) * 1000)

        sections_started = prog.get("sections_started_at")
        section_elapsed = 0.0
        if sections_started:
            section_elapsed = max(0.0, now - sections_started)
        effective = done + sum(
            section_partial(rec, now, prog.get("stage_expect") or None)
            for rec in (prog.get("running") or {}).values())
        eta_ms = None
        if total > 0 and effective > 0 and (total - effective) > 0:
            if section_elapsed > 0:
                eta_ms = int(section_elapsed / effective * (total - effective) * 1000)
            else:
                # 章节阶段尚未开始：用任务总耗时近似（已扣暂停）
                eta_ms = int(elapsed_ms / effective * (total - effective))

        return {
            "elapsed_ms": elapsed_ms,
            "eta_ms": eta_ms,
            "done": done,
            "total": total,
            "failed": failed,
            "words": words,
            "avg_section_ms": avg_section_ms,
            "concurrency": concurrency,
            "running": running,
            "phase": phase,
            "phase_label": phase_label,
            "progress": progress,
        }
    except Exception:
        # 统计只服务于展示，任何异常都不允许影响生成主流程
        return {}


def make_content_progress_state(concurrency: int, phase: str = "prepare",
                                phase_label: str = "准备中") -> dict:
    """构造正文生成的实时进度状态容器（_prog 初始值）。"""
    return {
        "started_at": time.monotonic(),
        "total": 0,
        "done": 0,        # 已处理章节数（含成功与失败）
        "failed": 0,
        "words": 0,       # 已落库正文累计字数
        "running": {},    # section_id -> {title, index, stage, stage_label, started_at, stage_started_at}
        "section_ms": [], # 每章实测耗时（秒），用于「平均单章耗时」统计
        "concurrency": concurrency,  # 并发档位（严格 = 用户选择）
        "phase": phase,
        "phase_label": phase_label,
        "sections_started_at": None,  # 章节阶段起点（ETA 外推口径）
        "stage_expect": {},           # 阶段耗时在线校准值
        "progress_max": 0.0,          # 对外进度历史最大值（monotonic 护栏）
    }

