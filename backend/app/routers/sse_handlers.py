"""SSE 事件处理器（核心：目录生成、正文生成、全局事实提取）

所有长任务通过 SSE 推送进度，支持暂停/恢复/停止。

控制机制：
- pause_event（默认 set=运行中）：循环顶部 await wait_resume() 挂起
- stop_event（默认未 set）：每轮检查 is_stopped() 并触发 asyncio.CancelledError
- 停止时通过 child_tasks 集合统一 cancel 所有并发子任务
"""
import asyncio
import copy
import json
import logging
import re
import sqlite3
import time

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

# ---------- SSE 心跳机制 ----------
# ✅ 单一权威实现（2026-09-15）：心跳包装器统一由 app.services.ai.sse_utils 提供。
#    旧实现本文件内另有一份「无界队列」副本，与 sse_utils 版本（maxsize=100 背压）
#    长期分叉；现统一 import，并新增 stats_provider —— 长 AI 调用期间由心跳通道
#    周期性推送运行统计（已耗时/进行中章节/累计字数/ETA），解决进度条静止问题。
from app.services.ai.sse_utils import with_heartbeat  # noqa: E402

from app.db import get_db, get_conn
from app.config import settings
from app.services.ai.json_response import collect_json_response, renumber_outline
from app.services.ai.provider_factory import chat_with_fallback
from app.services.ai.prompts._registry import render
from app.services.outline_utils import (
    MAX_OUTLINE_DEPTH, clamp_outline_depth, normalize_outline,
)
# ✅ 编号统一（2026-09-25）：收敛到 services/numbering —— 目录编号的唯一事实源。
# 导入三件套：
#   stored_outline_id     存储态编号读取（UUID 永不泄漏进提示词）
#   stored_id_to_display  存储态 → 展示态（第X章 / N / N.M），上级链与导出一致
#   strip_outline_numbering 标题内嵌编号剥离（与 json_response 同源再导出）
from app.services.numbering import (
    stored_outline_id, stored_id_to_display, strip_outline_numbering,
)
from app.db import settle_global_conn
from app.services import activity_broadcaster as _ab
from app.services.ai import task_registry as _tr
from app.services.ai.task_registry import (
    register_task, update_progress, finish_task,
    request_control,
    wait_resume, is_stopped, register_child_task, has_active_task,
    update_task_stats,
)
from app.db import get_read_conn, release_read_conn
from app.services.ai.workflows_base import concurrency_controller
from app.routers._chart_pipeline import (
    apply_inline_chart_plan, build_inline_chart_plan, _load_scheme_type_counts,
)
from app.services.content_utils import (
    select_target_leaves, build_sibling_context, word_status_for, text_word_count,
    max_tokens_for_budget,
    DEFAULT_WORD_BUDGET, WORD_UNDER_RATIO, WORD_OVER_RATIO,
    order_sections_dfs, leaf_word_budget, resolve_concurrency,
    auto_fix_unclosed_fences, normalize_word_budget_override,
)
from app.services.content_polish import quality_issues, sanitize_ai_content
from app.services.content_shrink import (
    shrink_content_rounds, SHRINK_MAX_ROUNDS,
)
from app.services.standards_registry import get_standards_text
# F-CONTENT-STANDARD(2026-09-26): generation standard (precise/fuzzy)
from app.services.content_standard import (
    resolve_effective_standard, normalize_standard, PRECISE,
    build_system_block, build_user_block, build_facts_header, build_continue_hint,
    standard_report,
)

logger = logging.getLogger("sse")
router = APIRouter(prefix="/api/v1/sse", tags=["sse"])

DANGEROUS_TYPES = {"深基坑", "高支模", "脚手架", "塔吊", "施工电梯", "临时用电", "有限空间", "爆破", "暗挖"}

# ---------- 正文生成参数（稳定性 / 体验） ----------
# ✅ 单次章节生成的 provider 超时：AI 配置里常见 timeout=60s，对 1500+ 字的章节明显偏短，
#    线上表现为大量章节 ~62s 超时失败。这里统一放宽并允许降级链逐个尝试。
CONTENT_REQUEST_TIMEOUT = 300      # 单个 provider 的超时（秒）
CONTENT_TOTAL_TIMEOUT = 660        # 含降级链的总超时（秒）
# ✅ 目录生成超时：一级/短方案目录输出体量比单章正文大，沿用配置里的 60s
#    容易在弱模型/慢链路下超时失败；这里统一放宽（降级链仍会逐个尝试）。
OUTLINE_REQUEST_TIMEOUT = 180      # 单次目录 AI 调用的 provider 超时（秒）
# ✅ 增强审核/修复超时常量化：旧实现内联 30s/45s，修复轮输入=完整目录+建议、
#    输出=完整目录（体量与生成相当），45s 在弱模型下频繁超时 →"自动修复超时，
#    保留原目录"，审核-修复循环形同虚设。放宽至与生成链路同量级。
OUTLINE_REVIEW_TIMEOUT = 60        # 目录审核单次调用超时（秒）
OUTLINE_FIX_TIMEOUT = 120          # 目录审核修复单次调用超时（秒）
# ✅ 分步生成阈值（原为内联魔数 50000）：字数预算超过此值的方案改走
#    「一级 → 逐章二三级 → 审核」分步链路。一次性直出 5 万字以上方案的
#    完整三级目录，输出体量过大、弱模型下极易被截断或超时。
OUTLINE_STEPWISE_MIN_WORDS = 50000
# ✅ 审核提示词的节点预算：超出部分不送入审核（避免 token 爆炸）。
#    按前序优先保留，一级/二级轮廓始终完整。
OUTLINE_REVIEW_MAX_NODES = 150
# ✅ 审核-修复链路的目录节点上限：比生成链路（500）宽松。
#    长方案完整目录（30 章 × 20+ 节点极易 > 500）若沿用 500，
#    修复结果会被 _validate_outline 判为非法并整轮丢弃，
#    「按审核建议修复」形同虚设。
OUTLINE_FIX_MAX_NODES = 1200
# ✅ 修复结果的最小覆盖率（相对原目录节点数）：
#    低于该比例视为「模型截断/敷衍」，保留原目录 —— 宁可未修复，不可丢目录。
#    见 _outline_fix_looks_degraded。
OUTLINE_FIX_MIN_COVERAGE = 0.8
# ✅ 单章子目录生成的预期耗时（秒）：用于分步链路「当前章」的进度渐近填充。
OUTLINE_CHAPTER_EXPECT = 45.0
# ---------- 结构化摘要按小节比例截断（2026-09-23） ----------
#: 每个小节的保底字符数：低于此值该小节等于没保留
MIN_SECTION_CHARS = 150
#: 单个小节最多占用总预算的比例（巨节不得吃掉全部预算）
MAX_SINGLE_SECTION_RATIO = 0.35
#: checkpoint 写库瞬态失败的重试次数 / 退避基数（秒）
CHECKPOINT_WRITE_RETRIES = 2
CHECKPOINT_WRITE_BASE_DELAY = 0.2
#: 配额/认证类错误（402/403/404/429）是否仍重试。
#: O8（2026-09-21）：默认 **False** —— 这类错误重试几乎必然再失败
#: （余额不足、密钥失效、模型不存在），白白多烧一次配额与一轮超时；
#: 置 True 可恢复旧的「一律重试一次」行为（向后兼容开关）。
AI_RETRY_ON_QUOTA_ERROR = bool(
    getattr(settings, "ai_retry_on_quota_error", False))

#: 判定为「配额/认证类」的 HTTP 状态码与错误关键词。
#: ⚠️ **429 不在其中**：429 是限流（临时、可退避自愈），必须保持重试 ——
#:    把它与 402/403/404（余额不足 / key 失效 / 模型不存在，重试必败）
#:    混为一谈会让限流场景直接失败，削弱 provider 降级链的抗抖动能力。
_QUOTA_HTTP_CODES = (402, 403, 404)
_QUOTA_ERROR_KEYWORDS = ("402", "403", "404", "insufficient",
                         "quota exceeded", "unauthorized",
                         "forbidden", "not found", "余额", "配额")


def _is_quota_error(err: BaseException | str) -> bool:
    """判断异常是否属于「余额 / 认证 / 模型不存在」类**确定性**错误。

    这类错误重试不会自愈（余额不足、key 失效、模型 ID 写错），必须直接
    失败并切 provider。**429 限流不在此列** —— 它是临时状态，退避后
    重试通常即成功，必须保持重试。
    """
    text = str(err or "")
    for code in _QUOTA_HTTP_CODES:
        if f"HTTP {code}" in text or f" {code} " in text:
            return True
    low = text.lower()
    return any(k in low for k in _QUOTA_ERROR_KEYWORDS)


# ---------- 目录生成 · 并发 / 批处理 / 审核模式（2026-09-21 ~ 09-23 配置化） ----------
# 长方案逐章子目录的批内并发数。旧实现纯串行（30 章 × ~45s ≈ 22 分钟），
# 是长方案"看起来卡死"的最大来源；批间仍串行以保留跨章去重上下文。
# <=0 一律回退 1（串行），保证配置脏值不炸编排层。
OUTLINE_CHAPTER_CONCURRENCY = max(1, int(settings.outline_chapter_concurrency))
# 单次调用合并生成的章数。默认 1 = 逐章调用（与旧行为逐字一致），
# 调大可把调用次数降为 ⌈N/k⌉，代价是批内跨章去重上下文变弱。
OUTLINE_CHAPTER_BATCH_SIZE = max(1, int(settings.outline_chapter_batch_size))
# 审核模式：auto = 先跑程序化覆盖预检（全过则跳过 AI 审核 / 有缺失则外科补齐），
# always = 总是走完整 AI 审核（旧行为）。
OUTLINE_REVIEW_MODE = str(settings.outline_review_mode or "auto")
CONTENT_SECTION_RETRIES = 1        # 章节生成失败后的重试次数
CONTENT_RETRY_BACKOFF = 6.0        # 普通错误退避基数（秒）
CONTENT_RATE_LIMIT_BACKOFF = 20.0  # 429 限流退避基数（秒）
# ✅ P1-1（2026-09-17）：自动续写轮数上限。
#    旧实现最多 4 轮、且每轮内还带 CONTENT_SECTION_RETRIES 次重试 → 单章
#    最坏 1（首稿）+ 4×2（续写）= 9 次会话级 AI 调用；实测均值 12.5 次/章
#    （2080 次调用 / 166 章），是墙钟的线性乘数。
#    首稿已按目标字数折算 max_tokens（见 max_tokens_for_budget）后，
#    返工需求大幅下降，故收敛为 2 轮、且续写失败**不重试**（见下）。
CONTENT_CONTINUE_MAX_ROUNDS = 2

# ---------- 正文生成进度模型（2026-09-15 增强） ----------
# ✅ 背景：原实现的进度只有「已完成章数 / 总章数」一个口径，且只在整章落库后
#    才推一次事件。单章 AI 调用（首轮 + 最多 4 轮续写）可持续 1~5 分钟，期间
#    零业务事件 —— 长方案下进度条数分钟纹丝不动，用户误判「卡死」。
# ✅ 现改为三段式：
#    1) 章节内阶段事件 section_stage（context → draft → continue → persist）；
#    2) 按阶段权重折算的**平滑总进度**（进行中章节也贡献部分进度）；
#    3) 心跳通道周期性推送 stats（已耗时 / 进行中章节 / 累计字数 / ETA）。
# 章节生成阶段的进度上限：其余区间留给全文一致性扫描与定向修复。
_SECTION_PHASE_MAX = 0.95
# 章节内阶段进度模型（折算「进行中章节」贡献的部分进度）：
#   base   进入该阶段时「本章已完成」的比例 —— 累积值，保证阶段间单调递增
#   span   该阶段自身占本章的比例（各 span 合计 = 1.0）
#   expect 该阶段的预期耗时（秒），用于阶段内按已耗时做渐近填充
# ✅ 为什么需要 base/span/expect 三件套：只给「阶段权重」无法保证单调
#    （continue 0.30 < draft 0.55 会让进度倒退）；只给累积基准值又会让耗时最长
#    的 draft 阶段整段纹丝不动。base + span + 按时间渐近填充同时解决这两点。
_STAGE_MODEL = {
    "context":  {"base": 0.00, "span": 0.05, "expect": 3.0},
    "draft":    {"base": 0.05, "span": 0.55, "expect": 90.0},
    "continue": {"base": 0.60, "span": 0.30, "expect": 60.0},
    "persist":  {"base": 0.90, "span": 0.10, "expect": 12.0},
}
# 阶段内渐近填充上限：只填到 span 的 85%，给「阶段完成」留出可见的跳变
_STAGE_FILL_MAX = 0.85
_STAGE_LABELS = {
    "context": "构建上下文",
    "draft": "AI 生成中",
    "continue": "续写扩充中",
    "persist": "清洗与落库",
}
# AI 长调用期间推送运行统计的间隔（秒）
_STATS_PUSH_INTERVAL = 3.0

# ✅ 阶段耗时在线校准（2026-09-15 增强 · 承接上一轮报告遗留建议 #1+#3）：
#    _STAGE_MODEL.expect 是出厂经验值（draft 90s / continue 60s），与具体模型、
#    章节目标字数差异很大：
#      · expect 偏小 → 阶段内还没干完就填满 85%，之后进度条又长时间「静止」；
#      · expect 偏大 → 进度条几乎不动，同样退回「静止观感」。
#    这里用**本次任务实测的阶段耗时**做 EMA 校准（只在阶段切换点更新，见
#    _calibrate_stage_expect 的单调性说明），使填充速率贴合真实模型速度。
_STAGE_EXPECT_MIN = 5.0      # 校准下限（秒）：防止个别极快样本把预期压到近 0
_STAGE_EXPECT_MAX = 600.0    # 校准上限（秒）
_STAGE_EXPECT_ALPHA = 0.3    # EMA 系数：新样本权重（越小越稳，越大越跟手）


def _section_partial(rec: dict, now: float,
                     expect_overrides: dict | None = None) -> float:
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
    model = _STAGE_MODEL.get(stage, _STAGE_MODEL["draft"])
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
    frac = min(elapsed / expect, 1.0) * _STAGE_FILL_MAX
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
    base_expect = overrides.get(stage) or (_STAGE_MODEL.get(stage, {}) or {}).get("expect") or 0.0
    if base_expect <= 0:
        return
    new_expect = (1 - _STAGE_EXPECT_ALPHA) * base_expect + _STAGE_EXPECT_ALPHA * actual
    overrides[stage] = round(min(max(new_expect, _STAGE_EXPECT_MIN),
                                 _STAGE_EXPECT_MAX), 2)


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
    partial = sum(_section_partial(rec, now, overrides)
                  for rec in (prog.get("running") or {}).values())
    raw = (processed + partial) / total
    return round(min(raw, 1.0) * _SECTION_PHASE_MAX, 4)


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


def _snapshot_stats(prog: dict, paused_ms: float = 0.0) -> dict:
    """构造运行统计快照（供 stats 事件与心跳通道使用，绝不抛异常）。

    字段口径：
      elapsed_ms  本次任务已耗时；eta_ms 线性外推的剩余耗时（无法预估时为 None）
      done/total  已处理章节数 / 总章节数；failed 其中失败数
      words       已落库正文累计字数
      avg_section_ms 已完成章节的平均实测耗时（无样本为 None）
      running     进行中章节列表（标题 + 当前阶段 + 该章已耗时）
      concurrency 当前并发档位
      phase       阶段标识（prepare/sections/consistency）+ 中文标签
      progress    折算后的总进度（与 progress 事件同口径）

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
            running.append({
                "section_id": sid,
                "title": rec.get("title", ""),
                "index": rec.get("index", 0),
                "stage": rec.get("stage", ""),
                "stage_label": rec.get("stage_label", ""),
                "elapsed_ms": int((now - rec.get("started_at", now)) * 1000),
            })
        # 平均单章耗时：已完成（含失败）章节的实测均值，用于「还要多久」判断
        # ✅ 单位修复：section_ms 列表实际存的是【秒】（见 _prog 初始化与逐章 append），
        #    对外字段名是 *_ms —— 必须换算毫秒，否则前端展示「平均 20 毫秒/章」。
        durations = prog.get("section_ms") or []
        avg_section_ms = (int(sum(durations) / len(durations) * 1000)
                          if durations else None)
        # ETA：按「已处理 + 进行中部分贡献」的有效进度外推（口径见 docstring）
        eta_ms = None
        effective = done + sum(
            _section_partial(rec, now, prog.get("stage_expect") or None)
            for rec in (prog.get("running") or {}).values())
        if total > effective > 0:
            section_started = prog.get("sections_started_at")
            if section_started is None:
                # 兼容：调用方未记录章节阶段起点（老任务 / 单测）时退化为任务耗时。
                # elapsed_ms **已扣除暂停**，此处不得再减（否则二次扣减）。
                section_elapsed_ms = elapsed_ms
            else:
                # ✅ ETA 与 elapsed_ms 同口径：显式章节起点尚未扣暂停，需在此扣除
                #    （否则暂停后 ETA 会凭空多出暂停时长）。
                section_elapsed_ms = max(
                    int((now - section_started) * 1000) - int(_paused), 0)
            # 至少按 1s 估算：首个统计点（耗时≈0）不应给出 0 或天量 ETA
            section_elapsed_ms = max(section_elapsed_ms, 1000)
            eta_ms = int(section_elapsed_ms / effective * (total - effective))
        return {
            "elapsed_ms": elapsed_ms,
            "eta_ms": eta_ms,
            "done": done,
            "total": total,
            "failed": prog.get("failed", 0),
            "words": prog.get("words", 0),
            "avg_section_ms": avg_section_ms,
            "running": running,
            "concurrency": prog.get("concurrency", 0),
            "phase": prog.get("phase", ""),
            "phase_label": prog.get("phase_label", ""),
            "progress": _weighted_progress(prog),
        }
    except Exception:
        # 统计只服务于展示，任何异常都不允许影响生成主流程
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
            await update_progress(task_id, progress, message, event=event)
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
    return (f"{b}字（可接受 0.9~1.1 倍即 {int(b * 0.9)}~{int(b * 1.1)} 字；"
            f"硬上限 {int(b * 1.2)} 字，超出即视为不合格）")


def _should_auto_shrink(content: str, word_budget: int,
                        over_ratio: float = WORD_OVER_RATIO) -> bool:
    """是否需要对该章节跑「超字数自动压缩」（与 word_status='over' 同口径）。

    只看**正文字数**（剔除图表代码块，见 content_utils.text_word_count）——
    否则一张数百字符的 mermaid 图会把阈值顶过去，触发无意义的压缩调用。
    """
    try:
        wc = text_word_count(content or "")
        budget = int(word_budget or DEFAULT_WORD_BUDGET)
    except (TypeError, ValueError):
        return False
    if budget <= 0:
        return False
    return wc > budget * over_ratio


async def _await_with_stats(coro, push_stats, interval: float = _STATS_PUSH_INTERVAL):
    """等待 AI 协程完成，期间每 interval 秒调用一次 push_stats 推送运行统计。

    背景：原实现在 await AI 调用期间完全不产出业务事件（只有 10s 一次的心跳
    comment，且前端不解析），进度条长时间静止。这里改为轮询式等待，周期性
    推送 stats（已耗时 / 进行中章节 / 累计字数 / ETA）。

    异常与取消语义与直接 await 完全一致：
      - coro 抛异常 → 原样重新抛出（调用方的重试 / 降级逻辑保持不变）；
      - 外层被取消 → 内部协程同步取消，不留悬挂任务。
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                return task.result()
            await push_stats()
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
    "prepare":   {"base": 0.02, "span": 0.03, "expect": 4.0,  "label": "准备中"},
    "draft":     {"base": 0.08, "span": 0.47, "expect": 55.0, "label": "AI 生成目录"},
    "level1":    {"base": 0.08, "span": 0.17, "expect": 40.0, "label": "生成一级目录"},
    "sublevels": {"base": 0.25, "span": 0.45, "expect": 0.0,  "label": "生成二三级目录"},
    "review":    {"base": 0.70, "span": 0.20, "expect": 45.0, "label": "目录审核中"},
    "fix":       {"base": 0.90, "span": 0.10, "expect": 60.0, "label": "按审核意见修复"},
}

# ✅ 阶段耗时在线校准（2026-09-16 · 承接上一轮报告遗留建议 #1）：
#    上表的 expect 与 OUTLINE_CHAPTER_EXPECT 都是**出厂经验值**，与具体模型速度
#    差异很大（弱模型一级目录 150s、强模型 15s）。校准口径与正文生成的
#    _calibrate_stage_expect 完全一致：只用**本次任务实测耗时**做 EMA，且只在
#    阶段切换点写入（阶段内填充曲线保持连续，不会因 expect 变化而回退）。
_OUTLINE_EXPECT_MIN = 5.0      # 校准下限（秒）：防止个别极快样本把预期压到近 0
_OUTLINE_EXPECT_MAX = 600.0    # 校准上限（秒）
_OUTLINE_EXPECT_ALPHA = 0.3    # EMA 系数：新样本权重
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
    model = _OUTLINE_PHASE_MODEL.get(phase) or {}
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
    return OUTLINE_CHAPTER_EXPECT


def _calibrate_outline_expect(st: dict, phase: str, actual: float) -> None:
    """用实测阶段耗时 EMA 校准该阶段的预期耗时。

    ✅ 调用约定：**只在离开某阶段时**调用（见 _advance_outline_phase）。
    理由与正文生成 _calibrate_stage_expect 相同：expect 变小会让「同一时刻算出的
    阶段进度」变大、变大则相反 —— 阶段进行中反复调整会让进度条回退；只在切换点
    写入则阶段内曲线连续，跨阶段单调性由 base 累积（上一阶段完成值 = 下一阶段基准）
    保证，与校准值无关。

    actual 小于 _OUTLINE_CALIBRATE_MIN_SAMPLE 时视为无效样本（空转阶段），忽略。
    """
    if not phase or actual < _OUTLINE_CALIBRATE_MIN_SAMPLE:
        return
    base_expect = _outline_expect(st, phase)
    if base_expect <= 0:
        return
    overrides = st.get("stage_expect")
    if not isinstance(overrides, dict):
        overrides = {}
        st["stage_expect"] = overrides
    new_expect = (1 - _OUTLINE_EXPECT_ALPHA) * base_expect + _OUTLINE_EXPECT_ALPHA * actual
    overrides[phase] = round(min(max(new_expect, _OUTLINE_EXPECT_MIN),
                                 _OUTLINE_EXPECT_MAX), 2)


def _calibrate_outline_chapter_expect(st: dict, actual: float) -> None:
    """用实测单章耗时 EMA 校准「单章预期耗时」（决定 sublevels 阶段的填充速率）。"""
    if actual < _OUTLINE_CALIBRATE_MIN_SAMPLE:
        return
    base = _outline_chapter_expect(st)
    if base <= 0:
        return
    new = (1 - _OUTLINE_EXPECT_ALPHA) * base + _OUTLINE_EXPECT_ALPHA * actual
    st["chapter_expect"] = round(min(max(new, _OUTLINE_EXPECT_MIN), _OUTLINE_EXPECT_MAX), 2)


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
        _calibrate_outline_expect(st, prev, now - st.get("phase_started_at", now))
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
    p = _monotonic_progress(st.get("last_p", 0.0), _outline_progress(st))
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
    model = _OUTLINE_PHASE_MODEL.get(phase) or _OUTLINE_PHASE_MODEL["prepare"]
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
                chapter_expect = _outline_chapter_expect(st)
                if chapter_expect > 0:
                    cur = min(elapsed / chapter_expect, 1.0) * _STAGE_FILL_MAX
            frac = min((done + cur) / total, 1.0)
    else:
        # ✅ 阶段预期耗时走本任务 EMA 校准值（阶段切换时写入，阶段内取值恒定
        #    ⇒ 填充曲线连续、单调）
        try:
            expect = float(_outline_expect(st, phase))
        except (TypeError, ValueError):
            expect = 0.0
        if span > 0 and expect > 0:
            elapsed = max(0.0, now - st.get("phase_started_at", now))
            frac = min(elapsed / expect, 1.0) * _STAGE_FILL_MAX
        else:
            frac = 0.0
    return round(min(base + span * frac, 1.0), 4)


def _outline_phase_label(st: dict) -> str:
    """当前阶段的用户可读标签（progress 事件与 stats 快照共用，单一来源）。"""
    return (_OUTLINE_PHASE_MODEL.get(st.get("phase", "")) or {}).get("label", "")


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
        chapter_expect = _outline_chapter_expect(st)
        cur = 0.0
        if chapter_expect > 0:
            elapsed_cur = max(0.0, now - st.get("chapter_started_at", now))
            cur = min(elapsed_cur / chapter_expect, 1.0) * _STAGE_FILL_MAX
        effective = done + cur
        if effective <= 0:
            return None
        # ✅ 分母优先取「子目录阶段」起点（进入 sublevels 时写入）。
        #    缺失（旧任务/单测）时退回任务耗时，保持与历史口径兼容。
        base_at = st.get("sub_started_at") or st.get("started_at", now)
        span_ms = max(int((now - base_at) * 1000), 1000)   # ≥1s：首个统计点不给出 0
        return int(span_ms / effective * max(total - effective, 0.0))
    expect = _outline_expect(st, st.get("phase", ""))
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
        progress = _monotonic_progress(st.get("last_p", 0.0), _outline_progress(st))
        return {
            "elapsed_ms": elapsed_ms,
            "eta_ms": _outline_eta_ms(st, now, progress),
            "done": done,
            "total": total,
            "failed": len(st.get("failed_chapters") or []),
            "words": 0,
            "concurrency": 0,
            "running": [],
            "phase": st.get("phase", ""),
            "phase_label": _outline_phase_label(st),
            "progress": progress,
            "stepwise": bool(st.get("stepwise")),
            "nodes": st.get("nodes", 0),
        }
    except Exception:
        # 统计只服务于展示，任何异常都不允许影响生成主流程
        return {}


# ---------- 任务成果 checkpoint（SSE 断线/刷新后可恢复） ----------
# 说明：目录生成与正文生成都**只走 SSE**（成果需用户确认/落库后才持久化），
# 断线期间任务在后台跑完时，没有 checkpoint 即确定性丢失。
async def _save_task_checkpoint(task_id: str, kind: str, payload: dict):
    """把任务终态成果写入 task_registry.checkpoint_json（kind 区分任务类型）。

    payload 统一带 `event` 字段（completed/stopped/error），便于前端判断来源。

    ✅ 瞬态写失败自动重试（2026-09-23）：checkpoint 是断线重挂接的**唯一**
    数据源，一次 "database is locked" 就让用户刷新后拿不到任何成果。
    通过 _retry_db_locked 对 locked / disk I/O 退避重试（确定性错误不重试）。
    """
    await _retry_db_locked(
        lambda: _write_task_checkpoint_db(task_id, kind, payload),
        max_retries=CHECKPOINT_WRITE_RETRIES,
        base_delay=CHECKPOINT_WRITE_BASE_DELAY)


async def _write_task_checkpoint_db(task_id: str, kind: str,
                                   payload: dict) -> None:
    """把 checkpoint 真正写入 task_registry.checkpoint_json。

    独立成函数是为了让 `_save_task_checkpoint` 能整体重试（单测可 monkeypatch
    本函数注入"首次 locked、次次成功"来验证重试链路）。

    ✅ 重试理由（2026-09-23）：checkpoint 是**断线重挂接的唯一数据源**。
    正文生成并发写（多章并行落库 + task_registry 进度写）下，
    busy_timeout 耗尽会抛瞬态的 "database is locked"；一次写失败就意味着
    用户刷新页面后拿不到任何已生成成果（跑了几分钟白跑）。
    确定性错误（no such table 等）由 _retry_db_locked 直接抛出，不重试。
    """
    conn = await get_conn()
    await conn.execute(
        "UPDATE task_registry SET checkpoint_json=?, updated_at=? WHERE id=?",
        (json.dumps({"kind": kind, **(payload or {})}, ensure_ascii=False),
         datetime.now().isoformat(), task_id))
    await conn.commit()


async def _save_outline_checkpoint(task_id: str, payload: dict):
    """把目录生成终态成果（outline/review/failed_chapters）写入 checkpoint_json。"""
    await _save_task_checkpoint(task_id, "outline_result", payload)


async def _save_content_checkpoint(task_id: str, payload: dict):
    """把正文生成成果清单（done/failed_sections/word_count）写入 checkpoint_json。

    ✅ 增强（2026-09-16 · 承接上一轮报告遗留建议 #2）：正文内容本身已逐章落库
    （sections.content），但「本次任务生成了哪几章、哪几章失败、为什么失败」只在
    SSE 事件里出现过 —— 用户刷新页面 / 网络抖动 / 关标签页后，前端重挂接只能拿到
    `stopped` + failed_count，**无法告诉用户是哪几章失败**，用户只能逐章翻目录树。
    这里把清单落库，`GET /sse/task/{id}`（task_status）会以 `content_result` 回传。
    """
    await _save_task_checkpoint(task_id, "content_result", payload)


async def _load_task_checkpoint(task_id: str, kind: str) -> dict | None:
    """读取指定类型的任务成果 checkpoint（不匹配的 kind 返回 None）。"""
    conn = await get_read_conn()
    try:
        cur = await conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id=?", (task_id,))
        row = await cur.fetchone()
    finally:
        await release_read_conn(conn)
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and data.get("kind") == kind:
        return data
    return None


async def _checkpoint_partial_outline(task_id: str, outline, failed_chapters,
                                      *, event: str = "stopped") -> bool:
    """落库「部分成果」checkpoint（客户端断开/用户停止时的兜底）。

    ✅ 修复（2026-09-16）：长方案分步链路要跑数分钟，成果只走 SSE、用户确认后
    才入库。此前只有**显式 stopped 分支**会写 checkpoint —— 客户端断开（刷新
    页面/关标签/网络抖动）走的是 `finally` 兜底路径：任务被标记 stopped，但已
    生成的 N 章目录确定性丢失（前端 pollTaskUntilTerminal 读到 stopped 却拿不到
    outline_result，只能提示"后台任务已停止"）。

    返回是否成功落库（失败静默，绝不影响收尾流程）。
    """
    outline = outline or []
    if not outline and not failed_chapters:
        return False
    payload = {"event": event, "task_id": task_id, "outline": outline}
    if failed_chapters:
        payload["failed_chapters"] = list(failed_chapters)
        payload["failed_count"] = len(failed_chapters)
    try:
        await _save_outline_checkpoint(task_id, payload)
        return True
    except Exception:
        logger.warning("保存目录部分成果 checkpoint 失败（task=%s）", task_id, exc_info=True)
        return False


async def _load_outline_checkpoint(task_id: str) -> dict | None:
    """读取目录生成成果 checkpoint（薄封装，语义与 kind 校验见 _load_task_checkpoint）。"""
    return await _load_task_checkpoint(task_id, "outline_result")


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


def _allocate_char_budgets(lengths: list[int], budget: int) -> list[int]:
    """按各小节长度**按比例**分配字符预算，带保底与封顶（总和 ≤ budget）。

    背景（2026-09-23）：旧实现对整份文本做「头部优先切片」（`text[:budget]`），
    20 个提取项里第 6 项之后**整段消失** —— 后面的项目参数（监测、验收等）
    对目录生成完全不可见。现改为按小节比例分配，每个小节都保留代表内容。

    规则：
    - 每项保底 `min_section_chars`（小节太短就完全没信息）；
    - 单项封顶 `budget * max_single_ratio`（巨节不得吃掉全部预算）；
    - 未超预算时等比即恒等（原样返回，不会无谓截断）。
    """
    if not lengths:
        return []
    n = len(lengths)
    total = int(sum(lengths) or 0)
    budget = int(budget or 0)
    if budget <= 0:
        return [0] * n
    if total <= budget:
        return [int(x) for x in lengths]
    min_chars = min(MIN_SECTION_CHARS, max(1, budget // max(n, 1) * 2))
    cap = max(1, int(budget * MAX_SINGLE_SECTION_RATIO))
    # 保底先占位，剩余按长度比例分配
    base = [min(min_chars, int(x)) for x in lengths]
    remaining = budget - sum(base)
    if remaining <= 0:
        return base
    flexible = [max(0, int(lengths[i]) - base[i]) for i in range(n)]
    flex_total = sum(flexible)
    if flex_total <= 0:
        return base
    extra = [min(cap - base[i], int(remaining * flexible[i] / flex_total))
             for i in range(n)]
    return [base[i] + extra[i] for i in range(n)]


def _budgeted_truncate_sections(text: str, budget: int) -> tuple[str, dict]:
    """按小节比例截断长文本（保底 + 封顶），返回 (截断后文本, 统计报告)。

    统计报告 {"sections": 小节数, "truncated_sections": 被截断的小节数}。

    稳定性契约：**每个小节的标题都保留**（只截正文）—— 目录生成需要看到
    "有哪 20 个提取项"，即便每项只剩开头一句；否则尾部项会整段消失。
    """
    sections = _split_md_sections(text)
    lengths = [len(body) for _title, body in sections]
    allocs = _allocate_char_budgets(lengths, budget)
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


# ---------- 续写辅助：安全尾部截断 + 段落级去重 ----------
def _safe_tail(text: str, limit: int = 2000) -> str:
    """取正文尾部最多 limit 字（续写提示词的「前文结尾」上下文）。

    ✅ 两层围栏保护（BUG 修复）：
    旧实现只看**尾部片段内** ``` 的奇偶、并从「尾片段中第一个围栏之后」截取，
    有两类错误：
      1) 切点**落在代码块内部**时，尾部片段以代码正文开头、只含闭合围栏；
         跳过第一个围栏后仍把 Mermaid/chart-json 代码当散文送给模型 ——
         模型据此续写会把图表代码当正文复述；
      2) 正文**结尾块未闭合**（AI 输出被截断，围栏数为奇数）时，旧逻辑再切一刀
         仍会留下一个未闭合围栏，模型收到半个代码块后会"热心"补上闭合围栏
         并臆造图表内容，污染正文。
    现改为：
      · 用**整篇正文**判断切点奇偶（切点之前的 ``` 数为奇数 ⇒ 切点在代码块内）
        → 前移跳过该块剩余部分与闭合围栏，使上下文从散文开始；
      · 再对尾部做「文末未闭合块」裁剪 —— 围栏数为奇数说明最后一个块没有闭合，
        整块丢弃（宁缺勿滥），避免把半截代码当"前文"。

    行边界：非围栏场景下回退到最近换行，避免以半行开头（首行距切点 < 200 字时）。
    """
    if not text:
        return ""
    if len(text) <= limit:
        tail = text
    else:
        cut = len(text) - limit
        tail = text[cut:]
        if text[:cut].count("```") % 2 == 1:
            # 切点落在代码块内部：跳过该块剩余内容与其**闭合**围栏。
            # 只吃掉紧随围栏的那个换行 —— 围栏之后即为散文，
            # 若按"跳到行尾"处理会把围栏后同一行/紧邻的散文一并删掉
            # （实测：围栏后紧跟散文时（无换行）会连散文一起吞掉）。
            nxt = tail.find("```")
            if nxt < 0:
                return ""            # 该块一直未闭合到结尾 → 无可用散文尾部
            tail = tail[nxt + 3:]
            if tail.startswith("\n"):
                tail = tail[1:]
        else:
            # 回退到最近的段落/换行边界（首行过长时保留，避免上下文过短）
            nl = tail.find("\n")
            if 0 <= nl < 200:
                tail = tail[nl + 1:]
    # 文末未闭合块裁剪：奇数围栏 → 丢弃最后一个未闭合块
    if tail.count("```") % 2 == 1:
        last = tail.rfind("```")
        tail = tail[:last] if last > 0 else ""
    return tail.strip()


def _dedup_continuation(prev: str, cont: str, min_overlap: int = 40) -> str:
    """检测续写内容与前文尾部的段落级重复，返回去除重复前缀后的续写文本。

    弱模型高频把最后一段原样重写一遍。按续写文本的段落前缀与前文尾部匹配，
    整段已在前文出现（≥min_overlap 字）即剥离；再处理续写开头与前文结尾
    的字符级重叠（模型从某句中间接续的场景）。

    ✅ BUG 修复（2026-09-16）：字符级重叠原实现用
    `for probe_len in range(min(300, len(b)), 40, -20)`（步长 20）试探
    「b 的前缀在 tail 中出现」——只有重叠长度恰好落在 len(b) − 20k 这一串
    离散点上才会命中，其余情况全部漏检（命题：真实文本的重复长度是任意的）。
    漏检的后果是续写把前文最后一段/句子原样复述一遍，正文出现成段重复。
    现改为**精确求最长重叠**（两层，均为 O(300×n) 的可忽略开销）：
      ① 接缝对齐（主路径）：b 的前缀恰好是 tail 的后缀 —— 模型重抄了刚看到的
         结尾再往下写，这是最常见的重复形态；
      ② 兜底：b 的长前缀在 tail 任意位置原文出现（弱模型跨段复述）。
    命中即剥离重叠部分（并去掉句首残留标点），无需再依赖步长运气。
    """
    if not cont:
        return cont
    # ✅ 健壮性：prev 可能为 None（调用方漏传/章节正文缺失），
    #    旧实现直接 prev[-3000:] 会抛 TypeError 并中断续写链路。
    tail = (prev or "")[-3000:]
    rest = cont.strip()
    # 逐段剥离：只要 cont 开头的整段已在前文尾部出现
    for _ in range(8):
        if not rest:
            break
        first_nl = rest.find("\n")
        head = rest if first_nl < 0 else rest[:first_nl]
        head_s = head.strip()
        if len(head_s) >= min_overlap and head_s in tail:
            rest = rest[first_nl + 1:].lstrip("\n") if first_nl >= 0 else ""
            continue
        break
    # 字符级：续写开头恰是前文结尾的延续重复（求最长重叠前缀）
    b = rest.lstrip()
    if len(b) >= min_overlap:
        max_probe = min(300, len(b))
        cut = 0
        for n in range(max_probe, min_overlap - 1, -1):
            if tail.endswith(b[:n]):
                cut = n
                break
        if not cut:
            for n in range(max_probe, min_overlap - 1, -1):
                if b[:n] in tail:
                    cut = n
                    break
        if cut:
            b = b[cut:].lstrip("，。；、\n ")
    return b or cont.strip()


# ---------- 辅助：项目关键事实（结构化全局事实）构建 ----------
def _facts_keywords(text: str) -> set[str]:
    """从章节标题/描述抽取关键字（中文 2-gram + ASCII 单词≥3），用于事实相关性预筛。"""
    import re
    kws: set[str] = set()
    if not text:
        return kws
    # 中文连续段 → 2-gram（最贴近中文切词、零依赖）
    for seg in re.findall(r"[一-鿿]+", text):
        for i in range(0, len(seg) - 1):
            kws.add(seg[i:i + 2])
    # ASCII 单词（≥3 字母，如 CFG、MJS、HDPE）
    for w in re.findall(r"[A-Za-z]{3,}", text):
        kws.add(w.lower())
    return kws


# 通用/总体类事实组：任何章节都应可见（项目级参数，不随章节主题消失）
_FACTS_GENERIC_GROUP_HINTS = ("概况", "总体", "通用", "项目信息", "工程概况", "项目概况", "编制依据")


def _filter_facts_rows(rows: list, leaf: dict) -> list:
    """按章节标题/描述对事实行做相关性预筛（对齐 OpenBidKit 的逐章精选注入）。

    - 无关键词（如章节无描述）→ 不过滤，返回全部（短方案/弱描述不退化）。
    - 通用类事实组始终保留（项目级参数）。
    - 命中任一关键词的事实保留。
    - 命中为空 → 回退全部（绝不因预筛丢事实）。
    """
    title = f"{(leaf.get('title') or '')} {(leaf.get('description') or '')}"
    kws = _facts_keywords(title)
    if not kws:
        return rows
    out: list = []
    for gt, t, content in rows:
        if any(h in (gt or "") for h in _FACTS_GENERIC_GROUP_HINTS):
            out.append((gt, t, content))
            continue
        hay = f"{gt or ''} {t or ''} {content or ''}"
        if any(kw in hay for kw in kws):
            out.append((gt, t, content))
    return out if out else rows


def _row_chapter(row) -> str:
    """从事实行取九大章节 key（历史 3/4 元组行无该列 → 返回空串）。

    `_load_facts_rows` 自 2026-09-24 起多读一列 chapter，行结构由固定
    3 元组变为 5 元组（gt, title, content, confidence, chapter）。
    历史上任何直接构造 3/4 元组行的调用方（含单测）必须继续可用，
    故按长度自适应而非索引硬取。
    """
    try:
        return str(row[4] or "") if len(row) >= 5 else ""
    except (TypeError, IndexError, KeyError):
        return ""


def _render_facts_text(
    rows: list, *, relevant_to: dict | None = None,
    max_total: int = 6000, per_fact: int = 300,
    chapter: str = "",
) -> str:
    """把 global_facts 行渲染为「项目关键事实」文本（纯函数，便于单测与逐章精选复用）。

    过滤规则（与正文生成一致，避免把待裁决/编造值当确定性事实注入）：
    按 group_title 分组聚合，每条事实截断 per_fact 字、总量截断 max_total 字
    （超量即停止，保留已写入小节的完整性）。

    ✅ 逐章精选（relevant_to，对齐 OpenBidKit 的逐章精选注入）：当传入章节 leaf 时，
    先按章节标题/描述关键字预筛相关性更高的事实；命中为空则回退全量（绝不丢事实）。
    """
    if relevant_to is not None:
        rows = _filter_facts_rows(rows, relevant_to)

    # ✅ 章节内事实前置（facts_chapter_inject，默认关闭）：把属于本章
    #    （chapter 匹配）的事实排到最前，其余保持原序跟在后面。
    #    稳定性契约：**绝不丢事实** —— 只是重排，不是筛选；超预算时
    #    被舍弃的只会是尾部（非本章）事实，本章事实必须优先保留。
    if chapter:
        hit: list = []
        rest: list = []
        for r in rows:
            (hit if _row_chapter(r) == chapter else rest).append(r)
        rows = hit + rest

    parts: list[str] = []
    total = 0
    _cur_gt = None
    for _row in rows:
        # 行结构自适应：3 元组（历史）/ 4 元组（+confidence）/ 5 元组（+chapter）
        gt, _title, content = _row[0], _row[1], _row[2]
        if gt != _cur_gt:
            header = f"### {gt}\n"
            # ✅ 修复（2026-09-16）：组标题也必须过预算 —— 旧实现无条件追写组标题，
            #    仅在末尾用 `[:max_total]` 切片兜底，于是超预算时**组标题会被截成
            #    "### 基坑支"** 这样的半截文本；且与 docstring 承诺的"保留已写入
            #    小节的完整性"不一致。现在所有写入都先过预算，末尾不再切片。
            if total + len(header) >= max_total:
                break
            parts.append(header)
            total += len(header)
            _cur_gt = gt
        fact = f"{(content or '')[:per_fact]}\n"
        # ✅ 低置信度标注（2026-09-23）：confidence 是 4 元组行的第 4 项
        #    （3 元组历史行无此列 → 不标注，向后兼容）。
        #    低于阈值时必须让模型知道"这条数字把握不大"，否则它会把
        #    低可信的提取值当作确定参数写进正文（实测基坑深度等关键
        #    参数偶尔只信到 0.3，成稿却写成确定值）。
        try:
            _conf = float(_row[3]) if len(_row) >= 4 and _row[3] is not None else None
        except (TypeError, ValueError):
            _conf = None
        if _conf is not None and _conf < LOW_CONFIDENCE_THRESHOLD:
            fact = f"（低置信度 {_conf:.2f}，请以工程实际为准）\n" + fact
        if total + len(fact) > max_total:
            # 超量即停止，保留已写入小节的完整性
            break
        parts.append(fact)
        total += len(fact)
    return "".join(parts)


async def _load_facts_rows(db, scheme_id: str) -> list:
    """读取方案下**可注入正文**的全局事实行（剔除矛盾值与未确认模拟值）。

    与 `_render_facts_text` 拆分的原因：正文生成需要对每个叶子章节做
    「逐章精选」（`relevant_to`），若逐章都查一次 DB，N 章就是 N 次查询；
    这里只查一次、在内存中按章过滤，兼顾正确性与性能。

    全局事实表缺失/查询异常时降级为空列表，绝不阻断生成。
    """
    try:
        # 方案事实 + 同项目的项目级事实（scheme_id 为空）一起注入，避免
        # “全局事实”在项目层保存后正文生成看不到。项目 ID 由方案反查，
        # 反查失败时安全回退为仅方案级查询。
        project_id = ""
        try:
            pcur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
            prow = await pcur.fetchone()
            if prow:
                project_id = str(prow[0] or "")
        except Exception:
            project_id = ""
        if project_id:
            sql = (
                "SELECT COALESCE(NULLIF(group_title,''), '其他事实') AS gt, "
                "title, content, confidence, chapter FROM global_facts "
                "WHERE (scheme_id=? OR (project_id=? AND "
                "(scheme_id='' OR scheme_id IS NULL))) "
                "AND has_conflict=0 AND is_resolved=1 ORDER BY gt, title"
            )
            params = (scheme_id, project_id)
        else:
            sql = (
                "SELECT COALESCE(NULLIF(group_title,''), '其他事实') AS gt, "
                "title, content, confidence, chapter FROM global_facts "
                "WHERE scheme_id=? AND has_conflict=0 AND is_resolved=1 "
                "ORDER BY gt, title"
            )
            params = (scheme_id,)
        cur = await db.execute(sql, params)
        return list(await cur.fetchall())
    except Exception as e:  # 全局事实表缺失/查询异常不应阻断生成
        logger.warning("构建项目关键事实失败（降级为无）: %s", e)
        return []


async def _build_facts_text(
    db, scheme_id: str, max_total: int = 6000, per_fact: int = 300,
    relevant_to: dict | None = None,
) -> str:
    """从 global_facts 构建结构化「项目关键事实」文本（目录生成 / 正文生成共用）。

    ✅ 增强：旧实现仅在正文生成内联构建 facts_text，目录生成完全缺失，
    导致目录无法引用已提取的设计参数（开挖深度、搭设高度、地质条件等），
    只能凭原始文档全文泛泛生成。现两路生成共用同一构建逻辑，事实口径一致。

    过滤规则（与正文生成一致，避免把待裁决/编造值当确定性事实注入）：
    - 跳过存在矛盾的事实（has_conflict=0）
    - 仅注入已审核确认（is_resolved=1）的事实 —— 提取结果默认 is_resolved=0 待审核

    ✅ 逐章精选（relevant_to）：传入章节 leaf 时按其标题/描述预筛相关事实
    （命中为空回退全量），用于降低长方案下全量注入的上下文膨胀。
    """
    rows = await _load_facts_rows(db, scheme_id)
    return _render_facts_text(
        rows, relevant_to=relevant_to, max_total=max_total, per_fact=per_fact)


async def _load_knowledge_rows(db, scheme_id: str) -> list[dict]:
    """加载方案可用的知识库条目行（专属 + 项目共享），一次查询供逐章内存过滤。"""
    try:
        cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
        row = await cur.fetchone()
        project_id = row["project_id"] if row else ""
        if not project_id:
            return []
        cur = await db.execute(
            "SELECT name, usage_hint, content FROM knowledge_base "
            "WHERE project_id=? AND (scheme_id=? OR scheme_id='')",
            (project_id, scheme_id))
        return [dict(r) for r in await cur.fetchall()]
    except Exception as e:  # 表缺失等异常不应阻断生成
        logger.warning("加载项目知识库素材失败（降级为无）: %s", e)
        return []


def _filter_knowledge_rows(rows: list[dict], leaf: dict) -> list[dict]:
    """按章节标题/描述对知识条目做相关性预筛（与事实逐章精选同规则）。

    无关键词或命中为空时回退全量，绝不因预筛丢失素材。
    """
    if not rows:
        return rows
    title = f"{(leaf.get('title') or '')} {(leaf.get('description') or '')}"
    kws = _facts_keywords(title)
    if not kws:
        return rows
    filtered = [
        r for r in rows
        if any(kw in f"{r.get('name','')} {r.get('usage_hint','')} "
                    f"{r.get('content','')}" for kw in kws)]
    return filtered or rows


async def _build_knowledge_text(db, scheme_id: str,
                                relevant_to: dict | None = None) -> str:
    """构建「项目知识库素材」文本（knowledge_base 表，产品需求 §3.9）。

    查询范围：该方案的专属条目（scheme_id=当前方案）+ 项目共享条目（scheme_id=''）。
    目录生成 / 正文生成共用；查询异常降级为空字符串，绝不阻断生成。

    ✅ 逐章精选（relevant_to）：与全局事实同一套 2-gram 关键字预筛规则，
    按章节标题/描述过滤相关素材，命中为空回退全量（长方案下避免把整本
    企业制度库无差别注入每一章，浪费 token 并稀释指令）。
    """
    from app.routers.knowledge import build_knowledge_text
    rows = await _load_knowledge_rows(db, scheme_id)
    if relevant_to is not None:
        rows = _filter_knowledge_rows(rows, relevant_to)
    return build_knowledge_text(rows)


# ---------- 辅助：递归构建 parent_chain ----------

# 中文数字映射（用于 level_tag 生成）
_CN_NUMBERS = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
               "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"]
_CN_DIGITS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _cn_number(n: int) -> str:
    """1-99 的序数 → 中文数字（章节号用）。

    ✅ 增强：旧实现只有到「二十」的静态表，超过 20 章的一级章节号会退化为
    阿拉伯数字（"第21章"），与文档其它位置的中文编号风格不一致。
    现 1-99 全量支持（21 → 二十一、30 → 三十、35 → 三十五），
    100 及以上仍回退阿拉伯数字（专项方案目录极少出现）。
    """
    if n <= 0:
        return str(n)
    if n < len(_CN_NUMBERS):
        return _CN_NUMBERS[n]
    if n >= 100:
        return str(n)
    tens, ones = divmod(n, 10)
    return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")


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
        stored = stored_outline_id(p)
        if stored:
            level_tag = stored_id_to_display(stored)
        else:
            # 历史脏数据（无 outline_json / id 非法）：按同胞位置折算，
            # 保证链里既不出现 UUID 也不出现空标签（" 标题"）。
            idx = _get_chapter_index(p)
            level_tag = f"第{_cn_number(idx)}章" if int(p.get("level") or 1) == 1 else str(idx)
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
        if (all(isinstance(v, int) and v > 0 for v in assigned.values())
                and sum(assigned.values()) == override):
            u["alloc"] = assigned
        else:
            base = override // len(unit_leaves)
            rem = override - base * len(unit_leaves)
            u["alloc"] = {
                lf["id"]: base + (1 if i < rem else 0)
                for i, lf in enumerate(unit_leaves)
            }
            logger.info("单元 %s 字数分配降级均分（每份 %d）",
                        u["unit"].get("title", "")[:20], base)


def _section_outline_number(sec: dict) -> str:
    """取章节目录编号（如 1.1.1），用于提示词中的『当前章节编号』。

    ✅ 编号统一（2026-09-25）：收敛到 services/numbering.stored_outline_id
    （唯一事实源）。旧实现在 outline_json 缺失/非法时回退到主键 id，
    而主键是 UUID —— 于是「没有待生成章节编号」的方案，其提示词里会出现
    一串 `550e8400-e29b-41d4-a716-446655440000`。模型会照抄进正文子标题，
    或据此误判层级（UUID 含连字符、不含点分段），成稿编号彻底失真。
    现一律返回合法点分路径，非法即空串（由提示词侧「编号缺失」兜底口径接管）。
    """
    return stored_outline_id(sec)


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
    return _section_outline_number(sec) or SECTION_NUMBER_MISSING


def _continue_failed_flag(continue_failed: bool,
                          word_count, word_budget) -> bool:
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
    return wc < int(wb * WORD_UNDER_RATIO)


async def _retry_db_locked(factory, max_retries: int = 3,
                           base_delay: float = 0.2):
    """对「database is locked / disk I/O error」做有限重试，其余异常原样抛出。

    背景（2026-09-23）：SQLite 在并发写 + busy_timeout 耗尽时会抛
    OperationalError("database is locked")。这类错误是**瞬态**的，退避后
    重试通常即成功；而把它与「no such table」这类确定性错误混在一起
    直接抛出，会让并发写场景出现难以复现的随机失败。

    只重试两类**瞬态**错误：
      · "database is locked"
      · "disk I/O error"
    其余 sqlite3.OperationalError（如 no such table）与非 sqlite3 异常
    一律原样抛出 —— 重试它们只是浪费时间并掩盖真实缺陷。

    退避为指数递增（base_delay × 2^尝试次数），总尝试次数 = 1 + max_retries。
    max_retries=0 即不重试。
    """
    attempts = max(0, int(max_retries))
    last_exc: Exception | None = None
    for i in range(attempts + 1):
        try:
            return await factory()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            transient = ("database is locked" in msg or "disk i/o error" in msg)
            if not transient:
                raise
            last_exc = e
            if i >= attempts:
                break
            await asyncio.sleep(base_delay * (2 ** i))
    if last_exc is not None:
        raise last_exc
    return await factory()


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
    from app.services import scheme_classification as _sc
    from app.services.facts_classification import TITLE_TO_CHAPTER
    bare = strip_outline_numbering(str(title)).strip()
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
    for alias, key in CHAPTER_TITLE_ALIASES.items():
        if alias in bare:
            return key
    return ""


async def _build_chapter_extraction_map(
        db, project_id: str, sections: list[dict],
        nodes: dict, children_by_parent: dict) -> dict[str, str]:
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
            "AND content != ''", (project_id,))
        rows = await cur.fetchall()
    except Exception:
        logger.warning("章节提取映射：读取结构化提取失败（退回原行为）", exc_info=True)
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
        for _ in range(20):          # 深度上限：脏数据成环时不至于死循环
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
            continue        # 顶层章节自身即叶子 → 不注入（见 docstring）
        title = str(top.get("title") or "")
        if not title:
            continue
        key = chapter_key_of_title(title)
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
    if max_nodes > 0 and _count_nodes(ol) > max_nodes:
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

    见 _validate_outline 的 strict_depth 说明。
    """
    return _validate_outline(obj, strict_depth=False)


def _outline_fix_validate_fn(obj) -> list[str]:
    """审核修复结果的校验函数：在生成链路校验基础上放宽节点数上限。

    ✅ BUG 修复：修复轮输入/输出都是**完整目录**（长方案 30 章 × 20+ 节点
    极易超过 500）。沿用生成链路的 500 上限时，修复结果会被一律判为非法 →
    走 "修复后校验仍不通过，保留原目录" 分支，"按审核建议自动修复" 整轮空转，
    用户看到的仍是未修正的目录。这里改用 OUTLINE_FIX_MAX_NODES。
    """
    return _validate_outline(obj, strict_depth=False, max_nodes=OUTLINE_FIX_MAX_NODES)


def _sublevel_validate_fn(o) -> list[str]:
    """分步链路「单章子目录」的校验函数。

    必须是非空节点数组，且每个节点都是含 title 的 dict。
    （弱校验 lambda 会放过 ["x", 1] 这类畸形数组，normalize 静默丢弃后
    章变空壳且不进 failed_chapters，用户看到一堆只有章标题的空章节。）

    ✅ 从 generate_outline 内的闭包提升到模块级：原实现定义在事件流闭包内，
    无法单测；该函数是「章变空壳」这一线上问题的主要拦截点。
    """
    ol = o.get("outline")
    if not isinstance(ol, list) or not ol:
        return ["outline 必须为非空数组"]
    bad = [n for n in ol if not isinstance(n, dict) or not str(n.get("title", "")).strip()]
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
    bad = [c for c in ncs
           if not isinstance(c, dict) or not str(c.get("title") or "").strip()]
    return [f"存在 {len(bad)} 个缺标题的非法章节"] if bad else []


def _outline_skeleton(nodes: list, max_nodes: int = OUTLINE_REVIEW_MAX_NODES) -> list:
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

    if _count_nodes(nodes) <= max_nodes:
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


def _coerce_bool(value, default: bool = False) -> bool:
    """把模型返回的"布尔"值稳健归一化为 bool。

    ✅ BUG 修复：旧实现用 `not review_obj.get("passed", True)` 直接判定审核结果，
    而弱模型常把 passed 写成字符串（"false" / "no" / "0"）。Python 中非空字符串
    恒为真 → "审核不通过"被误判为通过，"审核-自动修复"链路被整轮静默跳过，
    用户以为目录已按审核建议修正，实际原样返回。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1", "通过", "是", "pass", "passed", "ok"):
        return True
    if text in ("false", "no", "n", "0", "不通过", "否", "fail", "failed"):
        return False
    return default


def _coerce_suggestions(value) -> list[str]:
    """把模型返回的 suggestions 稳健归一化为 list[str]。

    ✅ BUG 修复：旧实现直接对 suggestions 做 `"; ".join(...)` 与
    `suggestions + [...]`：
      1) 模型把 suggestions 写成字符串时（很常见），`"; ".join("补监测方案")`
         会按"单个字符"拆分 → 修复提示词退化为 "补; 充; 监; 测; 方; 案"；
      2) 异常分支里的 `"字符串" + ["..."]` 抛 TypeError，且该语句位于 except
         块内，异常会逃逸出 _review_and_fix_outline，把整次目录生成打成失败。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                t = item.strip()
                if t:
                    out.append(t)
            elif isinstance(item, dict):
                for key in ("suggestion", "text", "item", "content", "value"):
                    v = item.get(key)
                    if v is not None and str(v).strip():
                        out.append(str(v).strip())
                        break
            elif item is not None:
                t = str(item).strip()
                if t:
                    out.append(t)
        return out
    text = str(value).strip()
    return [text] if text else []


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
    normalize_outline(preview)
    return preview


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
    for item in reversed(items):          # 从最近往回取
        text = str(item).strip()
        if not text:
            continue
        cost = len(text) + (2 if picked else 0)   # 分隔符 "; "
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
    known = {ic.SRC_PARSE, ic.SRC_FACTS, ic.SRC_SCOPE, ic.SRC_REQ,
             ic.SRC_BASIS, ic.SRC_STANDARDS}
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
        label = _TRUNC_LABELS.get(src, src)
        extra[src] = f"{label} {size} 字超本装配点预算 {budget_i} 字，已截断"
    return extra


def _stopped_payload(task_id: str, message: str = "用户已停止",
                     *, progress: float | None = None,
                     failed_sections: list | None = None,
                     **_extra) -> dict:
    """「用户已停止」SSE 事件的**统一**载荷构造（含失败明细）。

    2026-09-24 根因：此前 stopped 事件只带 message，而 completed 才带
    failed_sections —— 同一任务在「正常结束」与「用户停止」两条路径上
    前端拿到的明细口径不同，停止后前端日志区整片空白，用户无法知道
    停止前已有哪几章失败。此处把失败明细提升到所有终态路径的公共字段。
    """
    payload = {
        "event": "stopped",
        "task_id": task_id,
        "message": message,
        "failed_sections": list(failed_sections or [])[:50],
    }
    if progress is not None:
        payload["progress"] = progress
    payload.update(_extra)
    return payload


async def _run_input_coverage_audit(
        db, project_id: str, scheme_id: str, scope_items: list,
        requirements_text: str, sample_provider, *,
        scene: str = "outline") -> dict:
    """四项依据输入的差集审计（只读；失败降级为 WARNING，绝不阻断生成）。

    sample_provider：可调用对象（返回本次**真正注入**的提示词文本）或字符串。
    m1（2026-09-23）：支持 callable —— 审计要检查的是实际发出去的那份文本，
    而非重新拼一份（两者可能因截断 / 模式不同而不同）。
    m2（2026-09-23）：样本求值异常必须**抬升为 WARNING**（此前只落 DEBUG，
    生产环境根本看不到），日志含「差集审计失败」以便检索。
    """
    try:
        from app.services import input_coverage as ic
        sample = (sample_provider() if callable(sample_provider)
                  else sample_provider)
        inv = await ic.build_inventory(
            db, project_id, scheme_id,
            scope_items=list(scope_items or []),
            requirements_text=str(requirements_text or ""))
        report = ic.audit_prompt_coverage(inv, str(sample or ""))
        ic.log_coverage_audit(scene, scheme_id, report)
        return report
    except Exception:
        logger.warning("输入差集审计失败（不阻断生成，scene=%s）", scene,
                       exc_info=True)
        return {}


async def input_coverage_snapshot(scheme_id: str, db=Depends(get_db)) -> dict:
    """四项依据输入台账快照（前端「输入覆盖」面板的数据源）。

    方案不存在时 404 —— 快照无归属对象，返回空台账只会让前端显示
    "全绿"，掩盖"方案 id 传错"这类调用方 bug。
    """
    from app.services import input_coverage as ic
    from app.services.scheme_scope import extract_construction_scope
    cur = await db.execute(
        "SELECT project_id, name, config_json FROM schemes WHERE id=?",
        (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "方案不存在")
    project_id = row["project_id"]
    try:
        config = json.loads(row["config_json"] or "{}")
    except (TypeError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    scope_items = list(extract_construction_scope(str(row["name"] or "")))
    inv = await ic.build_inventory(
        db, project_id, scheme_id, scope_items=scope_items,
        requirements_text=str(config.get("requirements") or ""))
    by_source: dict = {}
    for src in (ic.SRC_PARSE, ic.SRC_FACTS, ic.SRC_SCOPE,
                ic.SRC_REQ, ic.SRC_BASIS, ic.SRC_STANDARDS):
        es = inv.by_source(src)
        by_source[src] = {"total": len(es),
                          "available": sum(1 for e in es
                                           if e.status == "available")}
    return {
        "scheme_id": scheme_id,
        "construction_scope": {"items": scope_items},
        "entries": [e.as_dict() for e in inv.entries],
        "degraded": list(inv.degraded),
        "summary": {
            "total": len(inv.entries),
            "available": sum(1 for e in inv.entries
                             if e.status == "available"),
            "by_source": by_source,
        },
    }


@router.get("/input-coverage/{scheme_id}")
async def input_coverage(scheme_id: str, db=Depends(get_db)) -> dict:
    """四项依据输入台账（前端「输入覆盖」面板）。"""
    return await input_coverage_snapshot(scheme_id, db=db)


# ---------- 提示词治理 · SSE 侧薄封装（2026-09-24 · G5 / G6） ----------

#: 句子边界（截断时在这些标点之后断开，尽量不切碎句子）
_SENTENCE_END_CHARS = "。！？；\n"
#: 全局事实低置信度阈值：低于此值在注入正文时标注「低置信度」
LOW_CONFIDENCE_THRESHOLD = 0.5


def _truncate_context(text: str) -> str:
    """按 context_length_limit 对上下文做**句子边界**截断。

    ✅ 默认关闭（limit=0 → 原样返回）：护栏而非默认行为。

    截断必须落在句子边界上：直接 `text[:limit]` 会把最后一句切成半截
    （"第二句内容很长很长很长很长很长"），模型据此续写会顺着半句话编，
    产出与上下文语义不连贯的正文。找不到边界时（超长单句）才硬切，
    并追加省略号提示内容被截断。
    """
    if not text:
        return text
    limit = int(getattr(settings, "context_length_limit", 0) or 0)
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    cut = -1
    for ch in _SENTENCE_END_CHARS:
        pos = window.rfind(ch)
        if pos > cut:
            cut = pos
    if cut > 0:
        return window[:cut + 1]
    return window + "…"

def _apply_prompt_context_budget(text: str) -> str:
    """按 prompt_context_budget 对已标注分段的上下文做预算削减。

    ✅ **默认关闭（budget=0 → 原样返回）**：这是护栏而非默认行为，
    开启会改变发给模型的文本（截断），必须由用户显式配置。
    实际分配逻辑在 services/prompt_governance.allocate_context_budget
    （按「全局事实 > 目录树 > 资料摘要 > 知识库」优先级水填），
    本函数只做开关判断与异常降级 —— 治理异常绝不能阻断生成。
    """
    budget = int(getattr(settings, "prompt_context_budget", 0) or 0)
    if budget <= 0 or not text:
        return text
    try:
        from app.services.prompt_governance import apply_context_budget
        return apply_context_budget(text, budget)
    except Exception:
        logger.warning("上下文预算削减失败（使用原文继续）", exc_info=True)
        return text


def _guard_external_material(text: str) -> str:
    """对外部资料段落加"只读数据"围栏并做凭据脱敏（prompt_injection_defense 开启时）。

    ✅ **默认关闭**：绝不影响既有生成行为。
    只加围栏 + 告警，**不改写资料正文** —— 与「数据真实性红线」一致
    （资料被改写就等于伪造了用户上传的内容）。
    """
    if not getattr(settings, "prompt_injection_defense", False) or not text:
        return text
    try:
        from app.services.prompt_governance import (
            guard_external_segments, redact_sensitive,
        )
        # ✅ 顺序：先加围栏（标注"这是只读资料"），再脱敏凭据。
        #    脱敏放最后 —— 只动凭据形态的片段（sk-/api_key= 等），
        #    **不碰 GB/JGJ 等标准编号**（误伤会污染技术依据）。
        guarded = guard_external_segments(text)
        _redacted, _n = redact_sensitive(guarded)
        if _n:
            logger.warning("外部资料中检出 %d 处疑似凭据，已脱敏", _n)
        return _redacted
    except Exception:
        logger.warning("外部资料防护失败（使用原文继续）", exc_info=True)
        return text


# ---------- 长方案分步：子目录取回执行单元（批内并发 / 批内合并） ----------

def _as_children(obj) -> list:
    """从 AI 响应中取 outline 数组（畸形/缺失一律回退空列表）。

    弱模型常回 {"outline": null} 或 {"outline": {...}}；不归一化会让
    上层把 dict 当 list 迭代（逐字符变成子章节标题）。
    """
    children = obj.get("outline") if isinstance(obj, dict) else None
    return children if isinstance(children, list) else []


async def _finish_stopped_with_partial(task_id: str, holder: dict) -> str:
    """目录生成「用户停止」的唯一收尾出口：携带已生成成果 + 落 checkpoint。

    返回待 yield 的 SSE 行（event=stopped）。

    为什么必须是唯一出口（2026-09-20 修复）：
    旧实现在 4 个停止分支发**裸** stopped 事件（不带 outline、不写
    checkpoint）—— 目录已生成 20 章时用户点停止，成果确定性丢失，前端
    只能提示"后台任务已停止"，用户白等几分钟。

    顺序契约：**先落 checkpoint → 再置终态 → 最后 yield**。反过来会出现
    「finish_task 已把任务 pop 出内存、checkpoint 尚未写入」的窗口，
    断线重挂的 GET /sse/task/{id} 读到 completed/stopped 却拿不到成果。

    holder 处理：读出成果后清空（防 finally 兜底重复落库）；无成果时
    **不写** checkpoint（避免用空成果覆盖上一次的有效 checkpoint）。
    """
    outline = holder.get("outline") or []
    failed = holder.get("failed_chapters") or []
    payload = {'event': 'stopped', 'task_id': task_id}
    if outline:
        payload['outline'] = outline
        payload['partial'] = True
        if failed:
            payload['failed_chapters'] = list(failed)
            payload['failed_count'] = len(failed)
        try:
            await _save_outline_checkpoint(task_id, payload)
        except Exception:
            logger.warning("保存目录停止 checkpoint 失败（task=%s）", task_id,
                           exc_info=True)
    # 清空 holder：防 finally 兜底把同一份成果再落一次库
    holder["outline"] = []
    holder["failed_chapters"] = []
    await finish_task(task_id, "stopped", "用户已停止")
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _fetch_chapter_children(
    index: int,
    chapter: dict,
    *,
    sub_prompt: list,
    task_id: str,
    sem,
    validate_fn,
    timeout: int,
    push_stats,
) -> tuple[str, list]:
    """取回单章的二三级子目录 —— 长方案分步链路的**执行单元**。

    返回 (status, children)：status ∈ {"ok", "failed", "stopped"}。

    关键不变��（与 bid_analysis.run_with_pause_gate 同源教训）：
    **暂停闸门必须排在并发信号量之前**。写成「先 async with sem 再
    wait_resume」时，暂停挂起期间在途章把许可全部占住，同批其余章节
    饿死 —— 表现为"点了暂停，整批卡住不动"。

    失败语义：首次失败**重试恰好一次**（重试前再过一次闸门与停止检查），
    两次均失败才判 failed（由编排层计入 failed_chapters）。
    停止语义：进入前 / 排队拿到许可后各复检一次 is_stopped，已停止则
    **不发起 AI 调用**（省一次配额，也避免"点了停止还在烧 token"）。
    """
    # ① 闸门在前：暂停期间不占许可
    await wait_resume(task_id)
    if is_stopped(task_id):
        return "stopped", []

    async def _attempt() -> tuple[str, list]:
        """首试 + 重试恰好一次。

        两处调用各自显式携带 json_mode / temperature / scene（审计归因
        要求首试与重试都计入 outline_sublevel；漏打会让 /ai/stats 的调用
        次数与实际消耗对不上）。
        """
        try:
            obj, _raw = await _await_with_stats(
                collect_json_response(
                    sub_prompt, validate_fn, timeout=timeout,
                    json_mode=True, temperature=0.2, scene="outline_sublevel"),
                push_stats)
            return "ok", _as_children(obj)
        except asyncio.CancelledError:
            raise
        except Exception as e:          # noqa: BLE001 - 兜住任意 provider 异常
            # ✅ O8/O9 闸门：配额 / 认证 / 模型不存在类错误（402/403/404/429）
            #    默认**不重试** —— 这类错误重试几乎必然再失败，白烧一轮配额
            #    与超时；`AI_RETRY_ON_QUOTA_ERROR=True` 可恢复旧行为。
            if not AI_RETRY_ON_QUOTA_ERROR and _is_quota_error(e):
                logger.warning("第%d章子目录生成失败（配额/认证类，不重试）: %s",
                               index + 1, e)
                return "failed", []
            logger.warning("第%d章子目录首次生成失败: %s，重试一次", index + 1, e)
            # 重试前再过一次闸门与停止检查：用户在首试失败后可能已点
            # 停止/暂停，此时不该继续烧配额。
            await wait_resume(task_id)
            if is_stopped(task_id):
                return "stopped", []
            try:
                obj, _raw = await _await_with_stats(
                    collect_json_response(
                        sub_prompt, validate_fn, timeout=timeout,
                        json_mode=True, temperature=0.2, scene="outline_sublevel"),
                    push_stats)
                return "ok", _as_children(obj)
            except asyncio.CancelledError:
                raise
            except Exception as e2:     # noqa: BLE001
                logger.warning("第%d章子目录重试仍失败: %s", index + 1, e2)
                return "failed", []

    if sem is not None:
        async with sem:
            # ② 拿到许可后复检：排队期间用户可能已点停止
            if is_stopped(task_id):
                return "stopped", []
            return await _attempt()
    return await _attempt()


async def _fetch_unit_children(
    unit_chapters: list,
    *,
    batch_prompt: list | None,
    single_prompts: list | None,
    task_id: str,
    sem,
    timeout: int,
    push_stats,
) -> tuple[str, list]:
    """取回一个「执行单元」内各章的子目录。

    unit_chapters: [(章序号 i, 章节点 dict), ...]
    single_prompts: 与 unit_chapters 等长的单章提示词列表（合并调用回退用）

    返回 (unit_status, per)：unit_status ∈ {"ok", "failed", "stopped"}；
    per 与 unit_chapters 等长，元素为 (章状态, children)。

    合并语义（OUTLINE_CHAPTER_BATCH_SIZE > 1 时）：
    - 单元内 1 章 → 直接委托 _fetch_chapter_children（逐字等于旧行为）；
    - 单元内多章 → 1 次批量调用；**缺章逐章回退单章**，批量整体失败
      （首试+重试）也逐章回退 —— 绝不整批判死（判死会让 1 章失败拖垮整单元）。
    """
    if is_stopped(task_id):
        return "stopped", []

    # 单元内单章：直接委托（保持与旧实现逐字一致）
    if len(unit_chapters) == 1:
        i, ch = unit_chapters[0]
        st, children = await _fetch_chapter_children(
            i, ch, sub_prompt=(single_prompts[0] if single_prompts else []),
            task_id=task_id, sem=sem, validate_fn=_sublevel_validate_fn,
            timeout=timeout, push_stats=push_stats)
        return ("stopped" if st == "stopped" else "ok"), [(st, children)]

    # 单元内多章：一次批量调用
    batch_obj = None
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            obj, _raw = await _await_with_stats(
                collect_json_response(
                    batch_prompt, _sublevel_batch_validate_fn, timeout=timeout,
                    json_mode=True, temperature=0.2, scene="outline_sublevel"),
                push_stats)
            batch_obj = obj
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:          # noqa: BLE001
            last_err = e
            logger.warning("批量子目录（第%d次，%d章）失败: %s",
                           attempt, len(unit_chapters), e)
    if batch_obj is None:
        logger.warning("批量子目录重试仍失败（%s），逐章回退单章调用", last_err)

    # 按 chapter_id 归位（章号以「章序号+1」的字符串为准，与提示词一致）
    by_id: dict[str, list] = {}
    if isinstance(batch_obj, dict):
        for c in batch_obj.get("chapters") or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("chapter_id") or "").strip()
            ol = c.get("outline")
            if cid and isinstance(ol, list):
                by_id[cid] = ol

    per: list = []
    for idx, (i, ch) in enumerate(unit_chapters):
        cid = str(i + 1)
        if cid in by_id:
            per.append(("ok", by_id[cid]))
            continue
        # 缺章（批量失败 / 模型漏回该章）→ 逐章回退，绝不丢章
        logger.info("批量响应缺第%s章，回退单章调用", cid)
        st, children = await _fetch_chapter_children(
            i, ch, sub_prompt=(single_prompts[idx] if single_prompts else []),
            task_id=task_id, sem=sem, validate_fn=_sublevel_validate_fn,
            timeout=timeout, push_stats=push_stats)
        per.append((st, children))
    unit_status = "stopped" if any(s == "stopped" for s, _ in per) else "ok"
    return unit_status, per


def _compact_outline_json(nodes: list, max_desc: int = 80) -> str:
    """把目录树渲染为**紧凑 JSON**（供审核修复轮作为「原始目录」输入）。

    ✅ 修复（2026-09-16）：旧实现 `json.dumps(outline, ensure_ascii=False)` 直接
    把**带 id/level/空 children/长描述**的完整目录塞进修复提示词 —— 长方案
    （30 章 × 20+ 节点，节点上限 1200）可轻易超过 10 万字符，必然超出模型上下文
    或触发超时，`outline_feedback_system`（审核建议回灌修复）整轮空转，用户看到的
    仍是未修正的目录。

    这里做**无损语义压缩**（只去掉对模型无用的字段，不删节点）：
      · 去掉 id / level / confidence 等程序生成字段；
      · description 截断到 max_desc（修复只需判断"这节写什么"，不需要全文）；
      · 空 children 不输出（省掉每节点 14 字符的 `"children":[]`）。
    实测（30 章 × 3 级真实目录）字符量约为原 JSON 的 **0.63~0.66**
    （描述越长收益越大），1200 节点目录可压到数万字符以内。
    """
    budget = [max(int(max_desc), 0)]

    def _walk(ns, depth: int = 0) -> list:
        out: list = []
        if depth > 10 or not isinstance(ns, list):
            return out
        for n in ns:
            if not isinstance(n, dict):
                continue
            item: dict = {"title": str(n.get("title", "")).strip()}
            desc = str(n.get("description", "")).strip()
            if desc and budget[0]:
                item["description"] = desc[:budget[0]]
            children = n.get("children")
            if isinstance(children, list) and children:
                kids = _walk(children, depth + 1)
                if kids:
                    item["children"] = kids
            out.append(item)
        return out

    try:
        return json.dumps({"outline": _walk(nodes)}, ensure_ascii=False)
    except (TypeError, ValueError):
        # 极端脏数据（循环引用等）不应阻断修复链路：退回空目录结构
        return '{"outline": []}'


def _outline_fix_looks_degraded(original: list, fixed: list) -> bool:
    """修复结果是否**退化**（节点数远少于原目录）。

    ✅ 修复（2026-09-16）：旧实现只做结构校验，通过后**整份替换**原目录 ——
    弱模型在修复轮只回一个"示例结构"（如 3 个节点）时，用户辛苦生成的几十章
    目录会被静默替换成 3 个节点（正文生成随即"章节全丢"）。这里给出覆盖率
    下限：修复结果节点数低于原目录的 OUTLINE_FIX_MIN_COVERAGE 时判定退化，
    调用方保留原目录（宁可未修复，不可丢目录）。
    """
    n_orig = _count_nodes(original) if isinstance(original, list) else 0
    n_fixed = _count_nodes(fixed) if isinstance(fixed, list) else 0
    if n_orig <= 0:
        return False
    return n_fixed < max(1, int(n_orig * OUTLINE_FIX_MIN_COVERAGE))


# ---------- 编制要求程序化覆盖预检 + 外科式补齐（2026-09-21） ----------

#: 危大工程必备一级章节的**关键词**（程序化检查用，不是完整标题 ——
#: 目录里写「基坑降水与支护」也算提到了相关概念，用关键词做"是否提到过"
#: 的宽松判定；宁可多走 AI 也不放过真实缺失）。
_DANGEROUS_REQUIRED_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("工程概况", ("工程概况", "工程概述")),
    ("编制依据", ("编制依据",)),
    ("施工计划", ("施工计划", "施工部署")),
    ("施工工艺技术", ("施工工艺", "工艺技术")),
    ("安全保证措施", ("安全保证", "安全保障")),
    ("人员分工", ("人员分工", "组织机构")),
    ("验收要求", ("验收",)),
    ("应急处置措施", ("应急",)),
    ("计算书及相关图纸", ("计算书", "图纸")),
    ("监测方案", ("监测",)),
)


def _split_requirement_items(requirements: str) -> list[str]:
    """把用户填写的「编制要求」拆成逐条条目（并剥离条目自带编号）。

    输入通常是评审要点清单：
        "1. 工程概况及特点\\n2）施工部署与进度计划\\n- 安全保证措施\\n短\\n"
    期望 → ["工程概况及特点", "施工部署与进度计划", "安全保证措施"]

    「短」这类 1 字条目被丢弃：拆分后仍无法做任何覆盖判定，
    留在 missing 里只会让外科补齐去"补"一条根本没有内容的条目。
    """
    if not requirements or not str(requirements).strip():
        return []
    items: list[str] = []
    for raw in str(requirements).splitlines():
        line = str(raw).strip().lstrip("-*·•").strip()
        if not line:
            continue
        # 剥离 "1." / "2）" / "（三）" / "一、" 等条目编号前缀
        line = strip_outline_numbering(line).strip()
        if len(line) < 2:      # 剥离后过短 → 不是有效条目
            continue
        if line not in items:   # 去重保序
            items.append(line)
    return items


def _common_run(a: str, b: str) -> str:
    """两个字符串的最长公共子串（朴素 DP，仅用于短标题比对）。"""
    if not a or not b:
        return ""
    best = ""
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > len(best):
                    best = a[i - cur[j]:i]
        prev = cur
    return best


def _check_requirements_coverage(
    requirements: str, outline: list, is_dangerous: bool = False,
) -> tuple[bool, list[str]]:
    """程序化检查目录是否覆盖了编制要求 / 危大工程必备章节。

    返回 (covered, missing)：
    - covered=True → 全部命中，可跳过 AI 审核（省 1~2 次调用）；
    - missing     → 未命中的条目清单（供外科式补齐使用）。

    判定策略（**保守方向**：拿不准一律推向 missing，多走 AI，
    绝不因误判覆盖而放过真实缺失）：
    1. 双向子串匹配：条目是标题的子串，或标题（≥3 字）是条目的子串
       —— 避免"计"这种单字误命中；
    2. 条目含并列分隔符（、/，/与/及）时按**片段**匹配：任一 ≥3 字片段
       命中即覆盖（"计算书、相关图纸" → 命中"计算书及相关图纸"）；
    3. 标题与条目有 ≥4 字公共子串时也判覆盖（减少无谓的 AI 打扰）；
    4. 危大工程额外逐个检查 10 个必备章节关键词。
    """
    titles: list[str] = []
    for n in (outline or []):
        if not isinstance(n, dict):
            continue
        t = str(n.get("title") or "").strip()
        if t:
            titles.append(t)

    def _hit(fragment: str) -> bool:
        frag = fragment.strip()
        if len(frag) < 2:
            return False
        for t in titles:
            if frag in t or (len(t) >= 3 and t in frag):
                return True
        return False

    missing: list[str] = []
    for item in _split_requirement_items(requirements):
        if _hit(item):
            continue
        parts = [p.strip() for p in re.split(r"[、，,和与及]", item)
                 if len(p.strip()) >= 3]
        if parts and any(_hit(p) for p in parts):
            continue
        if any(len(_common_run(item, t)) >= 4 for t in titles):
            continue
        missing.append(item)

    if is_dangerous:
        for label, keywords in _DANGEROUS_REQUIRED_KEYWORDS:
            if not any(kw in t for kw in keywords for t in titles):
                missing.append(f"{label}（危大工程必备章节）")
    return (not missing), missing


def _restore_descriptions(original: list, target: list) -> None:
    """把原目录的 description 按「(层级路径, 标题)」回填到合并修复结果上（就地）。

    背景：审核提示词吃的是 _outline_skeleton 截断版目录（description 截到
    60 字以省 token），模型据此产出的 fixed_outline 里 description 也是截断的。
    若直接采用，用户目录中每章说明都凭空短一截 —— 审核省了一次调用，
    却污染了全部章节描述。

    键用「(路径, 标题)」而非 id：模型回传的 id 可能是编号而非原 id。
    仅当目标描述为空、或短于原描述（典型截断特征）时才回填，
    模型新写的更长描述予以保留。
    """
    if not isinstance(original, list) or not isinstance(target, list):
        return

    def _collect(nodes: list, prefix: str, acc: dict) -> None:
        idx = 0
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            idx += 1
            path = f"{prefix}.{idx}" if prefix else str(idx)
            title = str(n.get("title") or "").strip()
            if title:
                acc[(path, title)] = str(n.get("description") or "")
            children = n.get("children")
            if isinstance(children, list) and children:
                _collect(children, path, acc)

    orig_desc: dict = {}
    _collect(original, "", orig_desc)
    if not orig_desc:
        return

    def _restore(nodes: list, prefix: str = "") -> None:
        idx = 0
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            idx += 1
            path = f"{prefix}.{idx}" if prefix else str(idx)
            title = str(n.get("title") or "").strip()
            src = orig_desc.get((path, title))
            if src:
                cur = str(n.get("description") or "")
                if not cur.strip() or len(cur) <= len(src):
                    n["description"] = src
            children = n.get("children")
            if isinstance(children, list) and children:
                _restore(children, path)

    _restore(target)


def _merge_patch_chapters(outline: list, new_chapters: list) -> list:
    """把外科式补齐产出的新章节合并进现有目录（按 insert_after 定位）。

    定位策略（**精确优先、歧义则追加末尾**，宁可位置不完美也不插错）：
    1. 标题**完全相等**的命中：唯一 → 插到它后面；多处同名 → 视为歧义，
       追加末尾（同名章节本就无法判断用户想挂在哪一章之后）；
    2. 无精确命中时退化为**互含匹配**（「安全文明施工」⊂「安全文明施工保证措施」）：
       同样只在唯一命中时插入，多处命中视为歧义 → 追加末尾；
    3. 都不满足 → 追加末尾。
    拿不准时追加末尾而非猜第一个位置：插错位置会让新章落在语义无关的
    章节之后，比追加末尾更难被用户一眼看出问题。

    合并后统一 renumber —— 位置即编号的唯一事实源；新章插入到中间时，
    其后所有章的编号都要顺移（否则导出编号断号/重号）。
    """
    merged = [n for n in (outline or []) if isinstance(n, dict)]
    for nc in (new_chapters or []):
        if not isinstance(nc, dict):
            continue
        title = str(nc.get("title") or "").strip()
        if not title:
            continue
        after = str(nc.get("insert_after") or "").strip()
        node = {k: v for k, v in nc.items() if k != "insert_after"}
        node["title"] = title
        children = node.get("children")
        node["children"] = children if isinstance(children, list) else []
        pos = -1
        if after:
            titles = [str(ex.get("title") or "").strip() for ex in merged]
            exact = [i for i, t in enumerate(titles) if t and t == after]
            if len(exact) == 1:                      # ① 精确且唯一
                pos = exact[0] + 1
            elif not exact:                          # ② 无精确 → 互含回退
                fuzzy = [i for i, t in enumerate(titles)
                         if t and (after in t or t in after)]
                if len(fuzzy) == 1:
                    pos = fuzzy[0] + 1
        if pos < 0:
            merged.append(node)
        else:
            merged.insert(pos, node)
    renumber_outline(merged)
    return merged


async def _try_outline_patch(
    outline: list, missing: list, *,
    scheme_name: str = "", scheme_type: str = "",
    project_brief: str = "", project_facts: str = "",
) -> list | None:
    """外科式补齐：只为缺失项生成新的一级章节（1 次小调用替代
    「AI 审核 + 整目录重写」2 次大调用）。

    返回合并后的新目录；失败 / 校验不通过返回 None
    （调用方回退完整 AI 审核链路）。
    """
    if not missing:
        return None
    try:
        prompt = render(
            "outline_patch_system",
            scheme_name=scheme_name,
            scheme_type=scheme_type,
            project_brief=(project_brief or "")[:1500],
            project_facts=(project_facts or "")[:1500],
            chapter_titles="; ".join(
                str(n.get("title") or "") for n in (outline or [])
                if isinstance(n, dict))[:2000],
            missing_items="; ".join(missing[:20]),
        )
        obj, _raw = await asyncio.wait_for(
            collect_json_response(
                [{"role": "system", "content": prompt}],
                _outline_patch_validate_fn,
                json_mode=True, temperature=0.2, scene="outline_fix"),
            timeout=OUTLINE_FIX_TIMEOUT)
        new_chapters = obj.get("new_chapters") if isinstance(obj, dict) else None
        if not isinstance(new_chapters, list) or not new_chapters:
            return None
        return normalize_outline(_merge_patch_chapters(outline, new_chapters))
    except asyncio.TimeoutError:
        logger.warning("外科式补齐超时（%ds），回退完整 AI 审核", OUTLINE_FIX_TIMEOUT)
    except Exception as e:      # noqa: BLE001
        logger.warning("外科式补齐失败，回退完整 AI 审核: %s", e)
    return None


async def _review_and_fix_outline(
    outline: list, scheme_type: str, is_dangerous: bool, project_brief: str = "",
    scheme_name: str = "", project_facts: str = "", requirements: str = "",
    phase_cb=None, construction_scope: str = "",
) -> tuple[list, dict]:
    """目录审核 + 超时保护的自动修复。返回 (outline, review_obj)。

    phase_cb: 可选同步回调 `fn(phase)`，在进入「审核」「修复」子阶段时调用，
    供调用方切换进度阶段（审核+修复最长 180s，期间调用方正 await 本协程、
    无法 yield 事件，进度改由心跳通道的 stats_provider 带出）。
    """
    if not outline:
        return outline, {"passed": False, "suggestions": ["目录为空，无法审核"]}

    def _notify(phase: str):
        """上报子阶段（回调异常绝不影响审核主流程）。"""
        if phase_cb is None:
            return
        try:
            phase_cb(phase)
        except Exception:
            logger.debug("目录审核阶段回调失败（已忽略）", exc_info=True)

    # ---------- C：程序化覆盖预检（OUTLINE_REVIEW_MODE=auto，默认） ----------
    # 省调用次数的关键路径：编制要求 / 危大必备章节若已被**确定性**覆盖，
    # 就不必再花 1~2 次大调用去问 AI「过不过」（AI 审核还会随机地
    # 对同一目录给出不同结论）。任一环节拿不准都回退完整 AI 审核。
    if OUTLINE_REVIEW_MODE != "always" and requirements:
        _covered, _missing = _check_requirements_coverage(
            requirements, outline, is_dangerous)
        if not _missing:
            logger.info("程序化覆盖预检全过，跳过 AI 审核（省 1~2 次调用）")
            return outline, {"passed": True, "review_mode": "programmatic",
                             "suggestions": ["✅ 程序化预检：编制要求与危大必备"
                                             "章节均已覆盖，跳过 AI 审核"]}
        if requirements or is_dangerous:
            _notify("fix")
            _patched = await _try_outline_patch(
                outline, _missing, scheme_name=scheme_name,
                scheme_type=scheme_type, project_brief=project_brief,
                project_facts=project_facts)
            if _patched:
                logger.info("外科式补齐完成（缺失 %d 项）", len(_missing))
                return _patched, {
                    "passed": True, "review_mode": "programmatic+surgical",
                    "suggestions": [
                        f"✅ 已按程序化预检外科式补齐 {len(_missing)} 项缺失"
                        f"（{'；'.join(_missing[:5])}）"]}
            # 补齐失败 → 落回完整 AI 审核链路（不写 review_mode，
            # 前端据此判断「审核依据仍是 AI 审核结论」）
    # 说明：程序化预检只在**用户填了编制要求**时启用。危大工程必备章节
    #   关键词检查随编制要求一并进行；未填要求时无可程序化判定依据，
    #   照旧走完整 AI 审核（否则危大项目会因关键词字面差异被误判缺失，
    #   白白多跑一次外科补齐调用）。

    review_prompt = render("outline_review_system",
                           scheme_name=scheme_name,
                           scheme_type=scheme_type,
                           # ✅ 必须传 construction_scope / scheme_basis：
                           #    模板含这两个占位符，未传时**字面量 {scheme_basis} 会
                           #    残留进发给模型的提示词** —— 模型要么把它当正文、
                           #    要么按未知变量编造内容，审核结论随之失真。
                           construction_scope=construction_scope,
                           scheme_basis=construction_scope,
                           is_dangerous="是" if is_dangerous else "否",
                           project_facts=project_facts or "",
                           outline_json=json.dumps(_outline_skeleton(outline), ensure_ascii=False))
    # ✅ 编制要求覆盖检查（对齐 OpenBidKit score-planning 的「评分大项必须映射为目录分支」）：
    #    用户在方案设置里填写的编制要求/评审要点，逐条对应目录章节；缺失即审核不通过。
    if requirements:
        review_prompt += (
            "\n\n【编制要求覆盖检查（最高优先级）】目录必须逐条覆盖以下编制要求；"
            "任何一条在目录中没有对应章节（或仅含糊带过），即判 passed=false，"
            "并在 suggestions 中逐条列出缺失项及建议补充的章节：\n" + requirements)
    _notify("review")
    try:
        review_obj, _ = await asyncio.wait_for(
            collect_json_response(
                [{"role": "system", "content": review_prompt}],
                lambda o: [] if "passed" in o else ["缺少 passed 字段"],
                json_mode=True, temperature=0.2, scene="outline_review"),
            timeout=OUTLINE_REVIEW_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("目录审核超时（%ds），跳过审核直接完成", OUTLINE_REVIEW_TIMEOUT)
        review_obj = {"passed": True, "suggestions": ["审核超时已跳过"]}
    except Exception as e:
        logger.warning("目录审核失败: %s，跳过审核直接完成", e)
        review_obj = {"passed": True, "suggestions": [f"审核失败: {e}"]}

    if not isinstance(review_obj, dict):
        # 弱模型可能把审核结果写成顶层数组/裸值，兜底避免后续 .get 抛 AttributeError
        review_obj = {"passed": True, "suggestions": ["审核结果结构异常，已跳过"]}
    # ✅ BUG 修复：统一把 passed / suggestions 归一化为 bool / list[str]
    #（字符串 "false" 不再被当作通过；字符串 suggestions 不再被按字符拆分）
    suggestions = _coerce_suggestions(review_obj.get("suggestions"))
    # ✅ 审核判定收紧（原实现 default=True 语义过宽）：
    #    模型漏填 passed 时，旧逻辑一律「视为通过」→ 审核形同虚设。
    #    现区分三种情形：
    #      - passed 缺失且给出了具体建议 → 按不通过处理（修复有明确输入）；
    #      - passed 缺失且无任何建议 → 视为通过（避免无依据地空跑一轮修复）；
    #      - passed 存在 → 按 _coerce_bool 归一化（"false"/"否"/0 等均判不通过）。
    _passed_raw = review_obj.get("passed")
    if _passed_raw is None:
        needs_fix = bool(suggestions)
    else:
        needs_fix = not _coerce_bool(_passed_raw, default=True)

    if needs_fix and not suggestions:
        # 判不通过却没给建议：旧实现直接静默跳过修复，用户拿到未修正的目录
        # 且看不到任何原因。这里补一条兜底建议，让修复轮有输入、日志有归因。
        suggestions = ["审核判定目录未通过但未给出具体建议，请按编制要求与危大工程规范补齐必要章节"]
        logger.info("目录审核判不通过但未给出建议，已注入兜底修复建议")

    if needs_fix:
        _notify("fix")

        # ---------- A：审核 + 修复合并（省 1 次大调用） ----------
        # 审核提示词第 10 条已要求模型在 passed=false 时同轮输出 fixed_outline。
        # 采用条件（三者全满足，缺一即回退独立修复调用）：
        #   ① 结构合法（_outline_fix_validate_fn）；
        #   ② 未退化（节点覆盖率 ≥ OUTLINE_FIX_MIN_COVERAGE）；
        #   ③ 描述可还原（模型回传的是 60 字截断骨架版，须恢复原文）。
        # 弱模型常只回一个"示例结构"，直接整份替换会把用户几十章目录换成
        # 三章（正文随之全丢）—— 这是最危险的失败模式，故宁可不合并。
        _merged_fixed = review_obj.get("fixed_outline")
        if isinstance(_merged_fixed, list) and _merged_fixed:
            _issues = _outline_fix_validate_fn({"outline": _merged_fixed})
            if not _issues and _outline_fix_looks_degraded(outline, _merged_fixed):
                _issues = [
                    f"审核轮合并修复结果节点数异常（{_count_nodes(_merged_fixed)}"
                    f" < 原目录 {_count_nodes(outline)} 的 "
                    f"{int(OUTLINE_FIX_MIN_COVERAGE * 100)}%）"]
            if not _issues:
                # ✅ 恢复原描述：审核输入是 _outline_skeleton 截断版，
                # 模型回传的 description 同样是截断的；按 (id, title) 回填原文。
                _restore_descriptions(outline, _merged_fixed)
                outline = normalize_outline(_merged_fixed)
                review_obj["passed"] = True
                suggestions = (["✅ 已根据审核意见自动修复（审核轮合并输出）"]
                               + suggestions)
                review_obj["suggestions"] = suggestions
                return outline, review_obj
            logger.warning("审核轮合并修复结果不可用，回退独立修复调用: %s", _issues[:2])

        async def _do_fix():
            # ✅ 统一提示词源：消费注册条目 outline_feedback_system（旧内联版与注册条目双份漂移，
            # 前端编辑该条目永不生效）
            # ✅ 修复（2026-09-16）：原来传入 json.dumps(outline) 完整目录（含
            #    id/level/空 children/长描述，长方案可超 10 万字符），必然超上下文
            #    或超时 ⇒ 修复轮整轮空转。改用 _compact_outline_json 压缩（同语义、
            #    字符量约降 60%），并记日志便于排查。命中路径由 json 保持合法。
            _orig_json = _compact_outline_json(outline)
            logger.info("目录修复轮输入：原目录 %d 节点 / 压缩后 %d 字符",
                        _count_nodes(outline), len(_orig_json))
            fix_prompt = render("outline_feedback_system",
                                original_outline=_orig_json,
                                review_suggestions="; ".join(suggestions),
                                scheme_name=scheme_name,
                                scheme_type=scheme_type,
                                project_brief=(project_brief or "")[:1500],
                                project_facts=(project_facts or "")[:1500])
            return await collect_json_response(
                [{"role": "system", "content": fix_prompt}],
                _outline_fix_validate_fn,
                json_mode=True, temperature=0.2, scene="outline_fix")

        try:
            fix_obj, _ = await asyncio.wait_for(_do_fix(), timeout=OUTLINE_FIX_TIMEOUT)
            fixed_outline = fix_obj.get("outline", []) if isinstance(fix_obj, dict) else []
            if not isinstance(fixed_outline, list):
                fixed_outline = []
            # ✅ BUG 修复：修复结果只做"结构性"校验（strict_depth=False），
            # 且节点上限放宽到 OUTLINE_FIX_MAX_NODES —— 修复轮输入/输出都是
            # 完整目录，长方案极易超过生成链路的 500 节点上限，沿用旧校验会把
            # 修复结果整份丢弃、"按审核建议修复"空转。
            validation_issues = _outline_fix_validate_fn({"outline": fixed_outline})
            # ✅ 修复（2026-09-16）：结构合法 ≠ 结果可用。弱模型常只回一个"示例
            #    结构"，旧实现会把它整份替换掉用户几十章的目录（正文生成的章节
            #    随之全丢）。这里追加节点覆盖率下限判定。
            if not validation_issues and _outline_fix_looks_degraded(outline, fixed_outline):
                validation_issues = [
                    f"修复结果节点数异常（{_count_nodes(fixed_outline)} < "
                    f"原目录 {_count_nodes(outline)} 的 {int(OUTLINE_FIX_MIN_COVERAGE * 100)}%）"
                ]
            if validation_issues:
                logger.warning("修复后目录校验仍不通过: %s", validation_issues[:3])
                suggestions = suggestions + ["修复后校验仍不通过，保留原目录"]
            else:
                # 修复结果落库前统一裁剪到三级 + 重排编号，与主链路保持一致
                outline = normalize_outline(fixed_outline)
                review_obj["passed"] = True
                # ✅ 保留原始审核建议（追加而非覆盖）：前端「审核结论」区块要展示
                #    本轮到底按哪些建议改的，旧实现直接替换成一句话，审核依据丢失。
                suggestions = ["✅ 已根据审核意见自动修复"] + suggestions
        except asyncio.TimeoutError:
            logger.warning("目录自动修复超时（%ds），跳过修复保留原目录", OUTLINE_FIX_TIMEOUT)
            suggestions = suggestions + ["自动修复超时，保留原目录"]
        except Exception as e:
            logger.warning("目录自动修复失败，保留原目录: %s", e)
            suggestions = suggestions + [f"自动修复失败: {e}"]

    # 统一回写为 list[str]，保证下游（SSE payload / 前端）拿到的形态稳定
    review_obj["suggestions"] = suggestions
    return outline, review_obj


# ---------- 目录生成 ----------

async def _build_structured_brief(db, project_id: str, raw_brief: str,
                                  max_chars: int = 4000) -> str:
    """构建目录生成用的「项目摘要」：优先消费结构化提取成果，回退原始摘录。

    数据来源：bid_analysis_items（「结构化提取」20 项的 success 结果），
    经 format_downstream_context 拼为结构化 Markdown；原始文档摘录作为
    附录补充（截 800 字），保证未覆盖的信息仍可见。

    ✅ 容错：结构化提取未跑过 / 表不存在 / 单条内容损坏时一律回退 raw_brief，
    不影响目录生成主流程。
    """
    try:
        cur = await db.execute(
            "SELECT item_id, label, output_type, content FROM bid_analysis_items "
            "WHERE project_id=? AND status='success' AND content IS NOT NULL "
            "AND content != ''", (project_id,))
        rows = await cur.fetchall()
        if not rows:
            return raw_brief
        from app.services.bid_analysis_service import (
            format_downstream_context, is_missing_result,
        )
        items: dict[str, dict] = {}
        for r in rows:
            content = r["content"] or ""
            output_type = r["output_type"] or "markdown"
            if is_missing_result(content, output_type):
                continue
            items[r["item_id"]] = {
                "item_id": r["item_id"],
                "label": r["label"] or r["item_id"],
                "output_type": output_type,
                "status": "success",
                "content": content,
            }
        if not items:
            return raw_brief
        structured = format_downstream_context(items)
        if not structured.strip():
            return raw_brief
        # ✅ 边界感知截断（m4 · 2026-09-23）：按小节比例分配预算后再做
        #    **边界感知**截断（truncate_to_boundary），不再硬切 —— 硬切会把
        #    小节或 ``` 围栏切成两半，模型看到半截代码块会当成坏数据忽略。
        from app.utils.text_splitter import truncate_to_boundary
        structured, rep = _budgeted_truncate_sections(structured, max_chars)
        structured = truncate_to_boundary(structured, max_chars)
        if rep.get("truncated_sections"):
            logger.info("结构化项目摘要按小节比例截断：%d/%d 小节被截（预算 %d 字）",
                        rep["truncated_sections"], rep["sections"], max_chars)
        appendix = f"\n\n【原始资料摘录】\n{raw_brief[:800]}" if raw_brief.strip() else ""
        # ⚠️ 不得在此再做 `[:max_chars+900]` 硬切片 —— 那会把刚按比例
        #    分配好的尾部小节再次砍掉（比例截断等于白做）。此处直接拼接。
        return structured + appendix
    except Exception:
        logger.warning("结构化项目摘要构建失败，回退原始资料摘录", exc_info=True)
        return raw_brief


async def _outline_input_audit(db, project_id: str) -> str:
    """目录生成的输入侧差集审计（只读，仅供日志/排查，不参与生成决策）。

    返回已成功完成结构化提取的条目名（分号连接，截断 30 条）。
    用途：用户报"目录漏了本该有的章节"时，可直接比对「提取到了什么」
    与「目录写出了什么」，无需手工查库。
    """
    try:
        cur = await db.execute(
            "SELECT item_id, label, status FROM bid_analysis_items "
            "WHERE project_id=? ORDER BY item_id", (project_id,))
        rows = await cur.fetchall()
        done = [str(r["label"] or r["item_id"]) for r in rows
                if (r["status"] or "") == "success"]
        return "；".join(done[:30])
    except Exception:
        logger.debug("目录输入差集审计读取失败（忽略）", exc_info=True)
        return ""


def _outline_construction_scope(scheme: dict) -> str:
    """方案名称解析 → 提示词用的【方案名称主要施工内容】文本。

    受 settings.outline_name_basis 控制（默认 True）。关闭时返回空串，
    提示词对应占位符渲染为空 —— 与该开关引入前的行为逐字一致。
    """
    if not getattr(settings, "outline_name_basis", False):
        return ""
    try:
        from app.services.scheme_basis import parse_scheme_basis
        return parse_scheme_basis(str(scheme.get("name") or "")).prompt_text()
    except Exception:
        logger.warning("方案名称解析失败（目录生成降级继续）", exc_info=True)
        return ""


def _outline_standards_text(scheme: dict) -> str:
    """按方案类别匹配的编制依据规范文本（供提示词引用，防杜撰编号）。

    受 settings.outline_standards_inject 控制（默认 True）；关闭或异常时
    返回空串，提示词对应占位符渲染为空。
    """
    if not getattr(settings, "outline_standards_inject", False):
        return ""
    try:
        return get_standards_text(
            str(scheme.get("type") or ""), str(scheme.get("name") or ""))
    except Exception:
        logger.warning("编制依据规范构建失败（目录生成降级继续）", exc_info=True)
        return ""


@router.post("/generate-outline/{scheme_id}")
async def generate_outline(scheme_id: str, request: Request, db=Depends(get_db)):
    # ⚠️ 404 校验必须留在路由体（HTTP 状态语义，前端与既有测试都依赖它）。
    #    但**装配类 await 一律不得出现在这里**（见下方 _assemble_context）。
    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    scheme = await cur.fetchone()
    if not scheme:
        from fastapi import HTTPException
        raise HTTPException(404, "方案不存在")
    scheme = dict(scheme)
    project_id = scheme["project_id"]
    project: dict = {}

    # ✅ 响应头先行（2026-09-23 · 30s 建连超时根因修复）：
    #    旧实现在返回 StreamingResponse 之前串行 await 约 8 次装配 DB 查询/写
    #    （结构化摘要、目录库参考 + ref_count 写库、事实/知识库构建）。DB 锁
    #    竞争下（busy_timeout=15s × 最多 4 次重试）最坏远超前端 sseFetch 的
    #    30s 建连超时 → 前端报「后端连接超时」，而任务注册/AI 调用尚未开始
    #    （症状：左侧显示后台未运行、无 AI 调用、日志无痕）。
    #    现把全部装配移入 _assemble_context，只在 event_stream 内部调用。
    _prog: dict = {
        "started_at": time.monotonic(),
        "phase": "prepare",
        "phase_started_at": time.monotonic(),
        "chapter_started_at": time.monotonic(),
        "sub_total": 0,        # 分步链路：一级章节总数
        "sub_done": 0,         # 分步链路：已完成子目录的章节数
        "sub_started_at": 0.0,  # 分步链路：子目录阶段起点（ETA 分母，不含准备/一级目录）
        "stage_expect": {},    # ✅ 阶段耗时 EMA 校准值（本任务内，见 _calibrate_outline_expect）
        "chapter_expect": 0.0,  # ✅ 单章子目录耗时 EMA 校准值（0 = 用出厂值）
        "failed_chapters": [],  # 子目录生成失败的章节标题
        "stepwise": False,
        "nodes": 0,            # 已生成目录节点数（增量累计，避免每章 O(n) 全量统计）
        "last_p": 0.0,         # 已推送的最大进度（保证单调不倒退）
    }
    # ✅ 断线兜底成果容器：客户端断开时生成器被 aclose，GeneratorExit 直接落在
    #    yield 处（不经过任何 except），只有 finally 能收尾。把「已生成的目录树 /
    #    失败章节」提升到闭包外的可变容器，finally 才能把它们写进 checkpoint，
    #    否则跑了数分钟的成果确定性丢失（前端只能提示"后台任务已停止"）。
    _partial_holder: dict = {"outline": [], "failed_chapters": []}
    # 装配产物：由 _assemble_context 在流内填充，event_stream 解包还原
    # （同名局部变量，下游 50+ 处引用零改动）
    _assembled: dict = {}

    def _stats_provider() -> dict:
        """心跳通道回调：返回目录生成运行统计快照（同步、绝不抛异常）。"""
        return _snapshot_outline_stats(_prog)

    def _outline_phase_cb(phase: str):
        """审核/修复子阶段回调（同步）：只更新内存状态。

        此刻调用方正 await 在审核协程上、无法 yield 事件，进度改由心跳通道
        的 stats_provider 以 ping 事件带出（每 10s 一次）。
        """
        _advance_outline_phase(_prog, phase)

    async def _assemble_context() -> None:
        """装配目录生成所需的全部上下文（**只在 SSE 流内调用**）。

        产物写入外层 _assembled 字典，由 event_stream 解包为同名局部变量。
        单个装配环节失败只降级不阻断（与旧实现的 try/except 口径一致）：
        任一环节拿不到数据时目录仍可生成，只是提示词里少一块上下文。
        """
        nonlocal project, _assembled
        # ✅ BUG 修复（文件解析 → 目录生成的数据传递）：旧实现在**过滤空值之前**
        #    就 LIMIT 5，且没有 ORDER BY。只要待解析/解析失败的文档恰好排在前 5 条
        #    （新上传尚未解析的文件就是这种形态），已解析资料一条都取不到 ——
        #    目录生成静默退化成"只有工程类型 + 项目名称"，用户上传的资料完全不可见。
        #    现统一走 global_facts.load_parsed_texts（非空条件下推 SQL + 固定排序）。
        try:
            cur = await db.execute("SELECT * FROM projects WHERE id=?", (project_id,))
            project_row = await cur.fetchone()
            # ✅ 修复：project 记录缺失时旧实现 dict(None) 直接 TypeError → 500
            project = dict(project_row) if project_row else {}
        except Exception:
            logger.warning("项目 %s 读取失败（目录生成降级继续）", project_id, exc_info=True)
        from app.routers.global_facts import load_parsed_texts
        try:
            docs = await load_parsed_texts(db, project_id, limit=5)
        except Exception:
            logger.warning("已解析文档读取失败（目录生成降级继续）", exc_info=True)
            docs = []
        raw_brief = ("\n".join(docs)[:4000] if docs else
                     f"工程类型：{project.get('engineering_type','')}，"
                     f"项目名称：{project.get('name','')}")
        # ✅ 跨模块数据传递接线（文件解析 → 结构化提取 → 目录生成）：
        #    旧实现只把「原始文档前 4000 字」当项目摘要 —— 原文截断点之后的
        #    关键参数（基坑深度、支护形式、工期）对目录生成不可见；且「结构化
        #    提取」（bid-analysis 20 项）的成果从未被任何下游消费。现优先消费
        #    结构化提取的 success 结果作为项目摘要，原始摘录降级为附录；
        #    未跑结构化提取时行为与旧版完全一致（回退 raw_brief）。
        try:
            project_brief = await _build_structured_brief(db, project_id, raw_brief)
        except Exception:
            logger.warning("结构化摘要构建失败（目录生成降级继续）", exc_info=True)
            project_brief = raw_brief

        # ✅ 防御：config_json 被其它路径写坏时降级为空配置，避免 SSE 请求以
        #    非流式 500 失败（前端此时收不到 error 事件，只能看到通用
        #    「SSE 请求失败」）。
        try:
            config = json.loads(scheme.get("config_json") or "{}")
        except (TypeError, ValueError):
            logger.warning("方案 %s 的 config_json 非法，已降级为空配置", scheme_id)
            config = {}
        if not isinstance(config, dict):
            config = {}
        # ✅ 编制要求/评审要点（对齐 OpenBidKit score-planning 的评分大项来源）：
        #    注入目录生成（一级必须逐条覆盖）与审核（缺失判不通过并自动修复补齐）。
        requirements_text = str(config.get("requirements") or "").strip()
        library_ids = config.get("library_ids", [])
        # ✅ G1/G2 修复：使用 outline_reference 服务构建「人/模型可读」的紧凑参考
        #    文本，而非把裸 JSON 直接塞进 prompt（同等信息量下 token 浪费近 3 倍）；
        #    默认 only_approved=True 只取「已通过」目录库，避免待审核/已停用库污染
        #    AI 生成（与 apply-and-save 的强约束口径一致）；命中的库累加 ref_count，
        #    修复「仅 apply-and-save 才 +1、AI 参考不计数」的统计失真。
        reference_outline = ""
        _bump_ids: list[str] = []
        try:
            from app.services.outline_reference import (
                build_reference_outline, build_category_reference_outline,
                bump_ref_count,
            )
            reference_outline, _hit_ids = await build_reference_outline(
                db, library_ids, only_approved=True)
            _bump_ids.extend(_hit_ids or [])
            # ✅ 按类别自动匹配目录库（scheme_auto_match_outline，默认关闭）：
            #    方案自身被分类为某类危大工程后，把该类别的**标准目录模板**
            #    追加到参考里。必须**增量追加**（不允许覆盖用户选库结果），
            #    且仅在命中非空文本时才计数 —— 否则 ref_count 虚增而参考
            #    从未进入提示词（2026-09-25 修复的正是这个静默失效）。
            if getattr(settings, "scheme_auto_match_outline", False):
                _cat_ids = [str(scheme.get("hazard_category") or "").strip()]
                _cat_ids = [c for c in _cat_ids if c]
                if _cat_ids:
                    _cat_text, _cat_hits = await build_category_reference_outline(
                        db, _cat_ids)
                    if _cat_text and _cat_text.strip():
                        reference_outline = (
                            (reference_outline + "\n\n" + _cat_text).strip()
                            if reference_outline else _cat_text)
                        _bump_ids.extend(_cat_hits or [])
            if _bump_ids:
                await bump_ref_count(db, _bump_ids)
        except Exception:
            logger.warning("目录库参考构建失败（目录生成降级继续）", exc_info=True)
        # ✅ 增强：注入结构化「项目关键事实」（与正文生成共用同一构建函数），
        #    使目录紧扣本项目的真实设计参数（开挖深度、搭设高度、地质条件、
        #    周边环境等），而非仅凭原始文档全文泛泛生成、脱离项目实际。
        try:
            project_facts = await _build_facts_text(db, scheme_id, max_total=3000)
        except Exception:
            logger.warning("项目关键事实构建失败（目录生成降级继续）", exc_info=True)
            project_facts = ""
        # ✅ 知识库注入：企业规范/工艺素材随事实一并注入目录生成
        try:
            _knowledge_text = await _build_knowledge_text(db, scheme_id)
        except Exception:
            logger.warning("知识库素材构建失败（目录生成降级继续）", exc_info=True)
            _knowledge_text = ""
        if _knowledge_text:
            project_facts = (project_facts + ("\n\n【项目知识库素材】\n"
                                              + _knowledge_text))[:6000]
        _assembled = {
            "project_brief": project_brief,
            "reference_outline": reference_outline,
            "requirements_text": requirements_text,
            "project_facts": project_facts,
            "is_dangerous": str(scheme.get("type") or "") in DANGEROUS_TYPES,
            "construction_scope": _outline_construction_scope(scheme),
            "standards_text": _outline_standards_text(scheme),
            "_audit_outline_inputs": await _outline_input_audit(db, project_id),
        }

    async def event_stream():
        task_id = await register_task("outline_generation", project_id, scheme_id)
        # ✅ 响应头先行：在流内做装配（见路由体注释）。失败只降级 ——
        #    _assemble_context 内部各环节自带 try/except，这里再兜一层，
        #    保证「装配异常」永远不会让整个 SSE 以非流式 500 失败。
        try:
            await _assemble_context()
        except Exception:
            # 只读降级：_assembled 保持空字典即可（各产物 .get 默认空串），
            # 不得在此赋值 —— 那会把 _assembled 变成 event_stream 的局部名，
            # 后续读取直接抛 UnboundLocalError（正是本次要修的那类缺陷）。
            logger.exception("目录生成上下文装配失败（降级为空上下文继续）")
        # 解包还原同名局部变量（下游 50+ 处引用零改动）
        project_brief = _assembled.get("project_brief", "")
        reference_outline = _assembled.get("reference_outline", "")
        requirements_text = _assembled.get("requirements_text", "")
        project_facts = _assembled.get("project_facts", "")
        construction_scope = _assembled.get("construction_scope", "")
        standards_text = _assembled.get("standards_text", "")
        _audit_outline_inputs = _assembled.get("_audit_outline_inputs", "")
        is_dangerous = bool(_assembled.get("is_dangerous"))
        # ✅ 修复内存泄漏：事件直接 yield，不再 subscribe 未消费的队列
        try:
            async def _push_stats():
                """推送一次运行统计（不写 DB；失败静默，不影响生成）。"""
                try:
                    await update_task_stats(task_id, **_snapshot_outline_stats(_prog))
                except Exception:
                    pass

            async def _set_phase(phase: str, msg: str, *, force: float | None = None,
                                 extra: dict | None = None) -> str:
                """切换目录生成阶段：更新状态 + 推送 progress 事件，返回待 yield 的 SSE 串。

                进度取「阶段模型折算值」与「已推送最大值」的较大者，保证单调不倒退。
                force 用于对齐既有的可见刻度（如 0.05 连接、0.55 目录已生成）；
                extra 用于随事件附带业务载荷（如长方案的目录树预览 outline）。
                ✅ 阶段切换统一走 _advance_outline_phase（顺带完成上一阶段的耗时校准 +
                计时基准重置），避免与审核子阶段回调两处各写一遍。
                """
                _advance_outline_phase(_prog, phase)
                p = _outline_push_value(_prog) if force is None else float(force)
                p = max(p, _prog.get("last_p", 0.0))
                _prog["last_p"] = p
                await update_progress(task_id, p, msg)
                payload = {
                    "event": "progress", "task_id": task_id,
                    "progress": p, "message": msg,
                    **_snapshot_outline_stats(_prog),
                }
                if extra:
                    payload.update(extra)
                return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

            await update_progress(task_id, 0.05, "正在连接 AI...", event="connecting")
            yield f"data: {json.dumps({'event':'connecting','task_id':task_id,'progress':0.05,'message':'正在连接 AI...'}, ensure_ascii=False)}\n\n"
            _prog["last_p"] = 0.05

            # ✅ 防御（2026-09-19 · 目录生成深度审查）：schemes.word_budget 虽有
            #    DEFAULT 30000，但历史行 / 直接改库 / 副本导入等场景下仍可能为
            #    NULL 或字符串。旧实现 `scheme.get("word_budget", 30000)` 在键存在
            #    但值为 None 时返回 None，随后 `None > OUTLINE_STEPWISE_MIN_WORDS`
            #    抛 TypeError → 被外层 except 吞成「目录生成失败」，用户白跑。
            #    统一稳健取整：脏值一律回退默认预算，绝不让分档判断崩主流程。
            try:
                word_budget = int(scheme.get("word_budget") or 30000)
            except (TypeError, ValueError):
                word_budget = 30000
            use_stepwise = word_budget > OUTLINE_STEPWISE_MIN_WORDS
            _prog["stepwise"] = use_stepwise

            if not use_stepwise:
                await wait_resume(task_id)
                if is_stopped(task_id):
                    yield await _finish_stopped_with_partial(task_id, _partial_holder)
                    return

                yield await _set_phase("draft", "正在生成目录...", force=0.1)

                sys_prompt = render("outline_short_system",
                    scheme_name=scheme.get("name", ""),
                    scheme_type=scheme.get("type", ""),
                    construction_scope=construction_scope,
                    standards_text=standards_text,
                    project_facts=project_facts or "")
                user_prompt = f"【方案名称】：{scheme.get('name','')}\n【方案类型】：{scheme.get('type','')}\n【项目资料摘要】：{project_brief[:3000]}\n"
                if construction_scope:
                    user_prompt += f"【方案名称主要施工内容】：{construction_scope}\n"
                if reference_outline:
                    user_prompt += f"【目录库参考】：{reference_outline[:2000]}\n"
                if is_dangerous:
                    user_prompt += "【注意：本方案为危大工程，目录必须包含全部必要章节】\n"
                if requirements_text:
                    user_prompt += (
                        "【编制要求（目录必须逐条覆盖，每一条都要有对应章节，缺失即为不合格）】：\n"
                        f"{requirements_text}\n")
                messages = [{"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_prompt}]

                # ✅ 进度增强：目录 AI 调用最长 180s，期间用 _await_with_stats
                #    每 3s 推送一次 stats（已耗时 / ETA / 当前阶段），进度条不再静止。
                obj, raw = await _await_with_stats(
                    collect_json_response(
                        messages, _outline_validate_fn,
                        timeout=OUTLINE_REQUEST_TIMEOUT,
                        json_mode=True, temperature=0.2, scene="outline_draft"),
                    _push_stats)
                outline = obj.get("outline", [])
                if not outline:
                    await finish_task(task_id, "failed", "AI 返回空目录")
                    yield f"data: {json.dumps({'event':'error','task_id':task_id,'message':'AI 返回空目录，请重试'}, ensure_ascii=False)}\n\n"
                    return
                # ✅ 目录限三级：硬性裁剪 + 统一重排编号/补全 children（提示词约束外的兜底保障）
                # ✅ 清理：此处原有函数内的 `from app.services.outline_utils import
                #    normalize_outline`（与模块顶部导入重复，徒增阅读干扰）
                outline = normalize_outline(outline)
                # ✅ 防御：模型返回的节点全非法（字符串/数字等）时裁剪后为空树，
                #    旧实现会把空目录带进审核（"目录为空，无法审核"）并 completed，
                #    前端只能显示"AI 返回空目录"。这里显式 failed，错误归因更准确。
                if not outline:
                    await finish_task(task_id, "failed", "AI 返回目录节点格式非法（裁剪后为空）")
                    yield f"data: {json.dumps({'event':'error','task_id':task_id,'message':'AI 返回目录节点格式非法，请重试'}, ensure_ascii=False)}\n\n"
                    return

                _prog["nodes"] = _count_nodes(outline)
                _partial_holder["outline"] = outline   # 断线兜底：短方案成果同样可恢复
                # ✅ 短方案一次性直出也实时推送 outline，保证「目录树」在生成阶段即同步更新
                #    （长方案分步路径已在每章 progress 事件中携带 outline，此处补齐短方案的一致性）
                yield await _set_phase("draft", "目录已生成，正在构建目录树...",
                                       force=0.55, extra={"outline": outline})

                await wait_resume(task_id)
                if is_stopped(task_id):
                    yield await _finish_stopped_with_partial(task_id, _partial_holder)
                    return

                yield await _set_phase("review", "目录生成完成，正在审核...")

                await wait_resume(task_id)
                if is_stopped(task_id):
                    yield await _finish_stopped_with_partial(task_id, _partial_holder)
                    return

                outline, review_obj = await _review_and_fix_outline(
                    outline, scheme.get("type", ""), is_dangerous, project_brief,
                    scheme_name=scheme.get("name", ""),
                    project_facts=project_facts,
                    requirements=requirements_text,
                    construction_scope=construction_scope,
                    phase_cb=_outline_phase_cb)
                # ✅ 断线兜底用「修复后」的成果（审核补齐的章节不应在断线恢复时丢掉）
                _partial_holder["outline"] = outline

                await update_progress(task_id, 1.0, "目录生成完成", event="completed")
                _short_payload = {'event': 'completed', 'task_id': task_id,
                                  'outline': outline, 'review': review_obj}
                # ✅ 收尾顺序统一（2026-09-16）：先落 checkpoint → 再置终态 → 最后
                #    yield 事件。旧实现是 yield 完才 finish_task —— 前端收到
                #    completed 后立即 break/断开连接时，生成器被 aclose，
                #    finally 兜底会把**已成功完成**的任务标记成 "stopped"
                #   （活动中心/任务列表显示"客户端断开，任务已终止"），与真实结果矛盾。
                try:
                    await _save_outline_checkpoint(task_id, _short_payload)
                except Exception:
                    logger.warning("保存目录生成 checkpoint 失败（task=%s）", task_id, exc_info=True)
                await finish_task(task_id, "completed", "目录生成完成")
                # ✅ BUG 修复（2026-09-16 · 依据运行库最近一次任务记录）：
                #    此处曾误写旧变量名 `_partial`（本轮改名 _partial_holder 时漏改），
                #    短方案链路生成成功后在收尾处抛
                #      NameError: name '_partial' is not defined
                #    → 被外层 except 捕获并把任务置为 failed，前端收到 error 事件而
                #    **拿不到一级目录确认闸门**（目录其实已生成、checkpoint 也已落库）。
                #    实测：task_registry 最新一条 outline_generation 即为此故障。
                _partial_holder["outline"] = []
                yield f"data: {json.dumps(_short_payload, ensure_ascii=False)}\n\n"
            else:
                # 长方案分步生成（clamp_outline_depth / normalize_outline /
                # MAX_OUTLINE_DEPTH 已在模块顶部导入）
                await wait_resume(task_id)
                if is_stopped(task_id):
                    yield await _finish_stopped_with_partial(task_id, _partial_holder)
                    return

                yield await _set_phase("level1", "正在生成一级目录...", force=0.1)
                sys_prompt = render("outline_level1_system",
                                    scheme_name=scheme.get("name", ""),
                                    scheme_type=scheme.get("type", ""),
                                    construction_scope=construction_scope,
                                    standards_text=standards_text,
                                    project_brief=project_brief[:2000],
                                    reference_outline=reference_outline[:1000] or "无",
                                    project_facts=project_facts[:1500] if project_facts else "")
                messages = [{"role": "system", "content": sys_prompt}]
                if construction_scope:
                    messages.append({"role": "user", "content":
                                     f"【方案名称主要施工内容】：{construction_scope}"})
                if requirements_text:
                    messages.append({"role": "user", "content": (
                        "【编制要求（一级目录必须逐条覆盖，每一条都要有对应一级章节，缺失即为不合格）】：\n"
                        + requirements_text)})
                # ✅ 进度增强：一级目录 AI 调用同样用 _await_with_stats 保活进度
                obj, _ = await _await_with_stats(
                    collect_json_response(messages, _level1_validate_fn,
                                          timeout=OUTLINE_REQUEST_TIMEOUT,
                                          json_mode=True, temperature=0.2,
                                          scene="outline_level1"),
                    _push_stats)
                level1 = obj.get("outline", [])
                if not level1:
                    await finish_task(task_id, "failed", "AI 返回空一级目录")
                    yield f"data: {json.dumps({'event':'error','task_id':task_id,'message':'AI 返回空一级目录，请重试'}, ensure_ascii=False)}\n\n"
                    return
                _prog["sub_total"] = len(level1)
                _prog["sub_done"] = 0
                _prog["nodes"] = _count_nodes(level1)
                yield await _set_phase("sublevels",
                                       f"一级目录生成完成（{len(level1)}章），正在生成二三级...",
                                       force=0.3)
                # ✅ ETA 口径：记录「子目录阶段」起点 —— 此后剩余时间按本阶段的
                #    实测速率外推，不再把准备阶段/一级目录 AI 的耗时摊到每章上。
                _prog["sub_started_at"] = time.monotonic()

                full_outline = []
                failed_chapters: list[str] = []
                stopped = False
                # ✅ 防御：审核环节在「中途停止」时会被跳过，此时 review_obj 未绑定。
                #    旧实现的 payload 分支恰好依赖「跳过审核 ⇒ stopped=True」的隐含
                #    不变量，任何后续改动都可能把它变成 NameError（整次生成失败）。
                review_obj: dict | None = None
                # ✅ 级间上下文增量维护（旧实现每章都遍历 full_outline 重建一次，O(n²)）：
                #    每章完成后把该章的二级标题追加进来即可。
                prior_l2: list[str] = []
                # ✅ 提示词里「其他章节标题」不随章变化，循环外算一次（旧实现每章重算）
                level1_titles = [str(c.get("title") or f"第{j + 1}章") for j, c in enumerate(level1)]

                # ---------- 批内并发编排（OUTLINE_CHAPTER_CONCURRENCY） ----------
                # 旧实现逐章**纯串行**：30 章 x ~45s ~ 22 分钟，是长方案
                # 「看起来卡死」的最大来源。现改为「批内并发 + 批间串行」：
                # 批内并发吃满许可，批间仍按章序推进以保留跨章去重上下文
                # （prior_chapters 依赖前序章的二级标题）。
                # 编排不变式：
                #   1) 批间先过暂停闸门 + 停止检查，再发起并发 gather；
                #   2) 用 asyncio.gather（保序）而非「完成序」迭代 —— 结果必须
                #      按章序号归位，否则 full_outline 章序会乱、编号全错；
                #   3) 单元内多章时按 OUTLINE_CHAPTER_BATCH_SIZE 合并为一次
                #      调用（默认 1 = 逐章，与旧行为逐字一致）。
                _sem = (asyncio.Semaphore(OUTLINE_CHAPTER_CONCURRENCY)
                        if OUTLINE_CHAPTER_CONCURRENCY > 1 else None)
                _merge_k = max(1, OUTLINE_CHAPTER_BATCH_SIZE)
                _total = len(level1)
                for batch_start in range(0, _total, _merge_k):
                    # 1) 批间：暂停闸门 -> 停止检查 -> 才发起并发
                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        stopped = True
                        break
                    unit_idx = list(range(batch_start,
                                          min(batch_start + _merge_k, _total)))
                    single_prompts: list = []
                    for i in unit_idx:
                        ch = level1[i]
                        if not ch.get("title"):
                            ch["title"] = f"第{i + 1}章"
                        other_titles = [t for j, t in enumerate(level1_titles)
                                        if j != i]
                        single_prompts.append(render(
                            "outline_sublevel_system",
                            chapter_id=str(i + 1),
                            chapter_title=ch.get("title", ""),
                            chapter_desc=ch.get("description", ""),
                            scheme_name=scheme.get("name", ""),
                            scheme_type=scheme.get("type", ""),
                            construction_scope=construction_scope,
                            standards_text=standards_text,
                            project_brief=project_brief[:1500],
                            other_outline=_join_tail_budget(other_titles, 1200),
                            prior_chapters=_join_tail_budget(prior_l2, 1500) or "无",
                            requirements=requirements_text[:1500] or "无",
                            project_facts=(project_facts[:1000]
                                            if project_facts else "")))
                    batch_prompt = None
                    if len(unit_idx) > 1:
                        chapters_text = "\n".join(
                            f"- chapter_id={i + 1}｜标题：{level1[i].get('title','')}"
                            f"｜说明：{level1[i].get('description','')}"
                            for i in unit_idx)
                        batch_prompt = [{
                            "role": "system",
                            "content": render(
                                "outline_sublevel_batch_system",
                                chapter_count=len(unit_idx),
                                scheme_name=scheme.get("name", ""),
                                scheme_type=scheme.get("type", ""),
                                construction_scope=construction_scope,
                                standards_text=standards_text,
                                project_brief=project_brief[:1500],
                                other_outline=_join_tail_budget(
                                    [t for j, t in enumerate(level1_titles)
                                     if j not in unit_idx], 1200),
                                prior_chapters=_join_tail_budget(prior_l2, 1500) or "无",
                                requirements=requirements_text[:1500] or "无",
                                project_facts=(project_facts[:1000]
                                                if project_facts else ""),
                                chapters_text=chapters_text)}]
                    # 2) 并发发起（gather 保序）
                    _prog["sub_done"] = batch_start
                    _prog["chapter_started_at"] = time.monotonic()
                    results = await asyncio.gather(*[
                        _fetch_unit_children(
                            [(i, level1[i]) for i in unit_idx],
                            batch_prompt=batch_prompt,
                            single_prompts=single_prompts,
                            task_id=task_id, sem=_sem,
                            timeout=OUTLINE_REQUEST_TIMEOUT,
                            push_stats=_push_stats)])
                    # 3) 按章序号归位（results[j] 对应 unit_idx[j]）
                    _unit_status, per = results[0]
                    for j, i in enumerate(unit_idx):
                        ch = level1[i]
                        if j >= len(per):
                            continue
                        st, children = per[j]
                        if st == "stopped":
                            stopped = True
                            break
                        ch["children"] = children
                        if st == "failed":
                            failed_chapters.append(ch.get("title", f"第{i+1}章"))
                            _prog["failed_chapters"] = list(failed_chapters)
                    if stopped:
                        break
                    for i in unit_idx:
                        ch = level1[i]
                        full_outline.append(ch)
                        _ch_title = str(ch.get("title", ""))
                        for _sub in (ch.get("children") or []):
                            if (isinstance(_sub, dict)
                                    and str(_sub.get("title", "")).strip()):
                                prior_l2.append(
                                    f"{_ch_title} / {_sub['title'].strip()}")
                    done = len(full_outline)
                    _prog["sub_done"] = done
                    # 单章耗时 EMA 校准：出厂值 45s 在网络波动/弱模型下会让
                    # 「当前章填充」迅速顶格后长时间静止，用本任务实测均速替换
                    _calibrate_outline_chapter_expect(
                        _prog, max(0.0, time.monotonic()
                                   - _prog.get("chapter_started_at", 0.0)))
                    _prog["chapter_started_at"] = time.monotonic()
                    # 节点数增量累计（只统计本批子节点），避免全量遍历 O(n^2)
                    _prog["nodes"] = _prog.get("nodes", 0) + sum(
                        _count_nodes(level1[i].get("children") or [])
                        for i in unit_idx)
                    # 断线兜底：每批完成后刷新「部分成果」快照
                    _partial_holder["outline"] = full_outline
                    _partial_holder["failed_chapters"] = list(failed_chapters)
                    # 不可回退护栏：进度取「阶段折算值」与「已推送最大值」较大者
                    p = _outline_push_value(_prog)
                    msg = f"已完成 {done}/{_total} 章子目录"
                    await update_progress(task_id, p, msg)
                    # 预览载荷由 _build_partial_preview 深拷贝 + 全树规范化构造，
                    # 保证与收尾 normalize_outline 的结构逐节点一致
                    _preview = _build_partial_preview(full_outline)
                    yield "data: " + json.dumps({
                        'event': 'progress', 'task_id': task_id,
                        'progress': p, 'message': msg,
                        'done': done, 'total': _total,
                        'outline': _preview,
                        **_snapshot_outline_stats(_prog),
                    }, ensure_ascii=False) + "\n\n"

                # ✅ 目录限三级：硬性裁剪 + 统一重排编号/补全 children（提示词约束外的兜底保障）
                full_outline = normalize_outline(full_outline)
                _prog["nodes"] = _count_nodes(full_outline)
                _partial_holder["outline"] = full_outline

                # ✅ 全章失败语义：AI 连续故障时每章都是"标题+空 children"，normalize 后
                # 仍非空，旧逻辑报 completed 引导用户锁定一份只有章标题的废目录。
                all_failed = (not stopped and bool(level1)
                              and len(failed_chapters) >= len(level1))
                if all_failed:
                    # ✅ 遗留修复（2026-09-16）：全部章节失败时旧实现只发一个 error
                    #    事件、不写 checkpoint —— 长方案跑完数分钟，唯一成果（一级章
                    #    标题）确定性丢失。现把 full_outline（仅一级章标题）随 checkpoint
                    #    落库，SSE 断线/刷新后 GET /sse/task/{id} 仍可取回部分成果。
                    _fail_payload = {'event': 'error', 'task_id': task_id,
                                     'outline': full_outline,
                                     'failed_chapters': failed_chapters,
                                     'partial': True}
                    try:
                        await _save_outline_checkpoint(task_id, _fail_payload)
                    except Exception:
                        logger.warning("保存目录生成失败 checkpoint 失败（task=%s）", task_id, exc_info=True)
                    await finish_task(task_id, "failed",
                                      f"全部 {len(level1)} 章的子目录生成均失败（AI 服务异常），请重试")
                    _partial_holder["outline"] = []
                    yield ("data: " + json.dumps(
                        {'event': 'error', 'task_id': task_id,
                         'message': '全部章节子目录生成失败，请检查 AI 服务后重试',
                         'outline': full_outline,
                         'failed_chapters': failed_chapters,
                         'partial': True},
                        ensure_ascii=False) + "\n\n")
                    return

                # 审核环节（与短方案一致）；审核+修复最长约 3 分钟，进入前补停止检查点
                if not stopped and full_outline and not is_stopped(task_id):
                    yield await _set_phase("review", "目录生成完成，正在审核...")
                    _pre_review_nodes = _count_nodes(full_outline)
                    full_outline, review_obj = await _review_and_fix_outline(
                        full_outline, scheme.get("type", ""), is_dangerous, project_brief,
                        scheme_name=scheme.get("name", ""),
                        project_facts=project_facts,
                        requirements=requirements_text,
                        construction_scope=construction_scope,
                        phase_cb=_outline_phase_cb)
                    # ✅ 审核/修复可能增删节点（补齐缺失章节），统计里的节点数需同步，
                    #    否则进度卡一直显示修复前的旧值。
                    _prog["nodes"] = _count_nodes(full_outline)
                    if _prog["nodes"] != _pre_review_nodes:
                        logger.info("目录审核修复后节点数变化：%d → %d",
                                    _pre_review_nodes, _prog["nodes"])
                    # ✅ 兜底：修复轮若返回空目录（模型输出异常且未触及覆盖率护栏，
                    #    例如原目录本身为空）绝不把空成果当"完成"发出去
                    if not full_outline:
                        full_outline = _partial_holder.get("outline") or []
                        _prog["nodes"] = _count_nodes(full_outline)

                # 审核/修复期间用户点了停止：收尾为 stopped
                if not stopped and is_stopped(task_id):
                    stopped = True
                final_status = "stopped" if stopped else "completed"
                finish_msg = "用户已停止" if stopped else (
                    f"目录生成完成（{len(failed_chapters)}章子目录生成失败）" if failed_chapters else "目录生成完成")
                # ✅ 终态进度收口：长方案最后一条 progress 停在审核/修复区间（≈0.9），
                #    而 task_registry.progress 会被「断线后重新挂接」的前端读到并展示。
                #    这里显式推到 1.0（与短方案路径的 completed 写法对齐）。
                if not stopped:
                    _advance_outline_phase(_prog, "fix")
                    await update_progress(task_id, 1.0, "目录生成完成")
                event_name = "stopped" if stopped else "completed"
                payload = {'event': event_name, 'task_id': task_id, 'outline': full_outline}
                if not stopped and full_outline:
                    payload['review'] = review_obj if isinstance(review_obj, dict) else {}
                # 增强：长方案分步生成时，在 completed 事件中明确携带失败章节列表，
                # 前端可据此高亮显示失败章节，提示用户手动补充或重新生成。
                if failed_chapters:
                    payload['failed_chapters'] = failed_chapters
                    payload['failed_count'] = len(failed_chapters)

                # ✅ 成果可恢复 + 收尾顺序（2026-09-16 修复）：
                #    先落 checkpoint → 再置终态 → 最后 yield 事件。
                #    旧顺序（finish_task → checkpoint → yield）存在两个窗口：
                #      ① finish_task 已把任务 pop 出内存、checkpoint 尚未写入时，
                #         断线重挂接的 GET /sse/task/{id} 读到 status=completed 却
                #         没有 outline_result → 前端判定"无成果"并 load()，成果丢失；
                #      ② 反向的窗口会让已成功的任务被 finally 兜底标成 stopped。
                try:
                    await _save_outline_checkpoint(task_id, payload)
                except Exception:
                    logger.warning("保存目录生成 checkpoint 失败（task=%s）", task_id, exc_info=True)
                await finish_task(task_id, final_status, finish_msg)
                _partial_holder["outline"] = []
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            yield await _finish_stopped_with_partial(task_id, _partial_holder)
        except Exception as e:
            logger.exception("目录生成失败")
            # ✅ 增强（2026-09-16 · 依据运行库「目录已生成却以 error 收场、成果无出口」的
            #    真实实例）：终态失败时把**已生成的部分成果**随 error 事件下发 ——
            #    前端 `doGenerateOutline` 早已实现「目录生成失败，但有部分成果 →
            #    是否保存」分支（由 `evt.outline` 触发），但 error 事件此前不带
            #    outline，用户只能看到一句报错、已生成的一级/多级目录白丢。
            _err_payload = {'event': 'error', 'task_id': task_id, 'message': str(e)}
            _err_outline = _partial_holder.get("outline") or []
            if _err_outline:
                _err_payload['outline'] = _err_outline
                _err_payload['partial'] = True
                _err_ch = _partial_holder.get("failed_chapters") or []
                if _err_ch:
                    _err_payload['failed_chapters'] = _err_ch
                    _err_payload['failed_count'] = len(_err_ch)
                try:
                    await _save_outline_checkpoint(task_id, {
                        'event': 'error', 'partial': True,
                        'outline': _err_outline, 'failed_chapters': _err_ch})
                except Exception:
                    logger.warning("保存目录失败成果 checkpoint 失败（task=%s）",
                                   task_id, exc_info=True)
            await finish_task(task_id, "failed", str(e))
            yield f"data: {json.dumps(_err_payload, ensure_ascii=False)}\n\n"
        finally:
            # ✅ 断开兜底：客户端断开时生成器被 aclose，GeneratorExit 直接落在
            # yield 处（不经过任何 except），任务会永远停在 running、_tasks 泄漏。
            # 此处幂等清理：仅当任务尚未进入终态（finish_task 未执行过）时兜底。
            # 注意：GeneratorExit 路径下 finally 中不允许 yield。
            if has_active_task(task_id):
                # ✅ 成果可恢复（2026-09-16 修复）：断线兜底收尾前，把已生成的部分
                #    成果（长方案：已完成章的目录树；短方案：完整目录）写进 checkpoint。
                #    旧实现只标 stopped 不落库 → 前端 pollTaskUntilTerminal 拿到
                #    stopped 却取不到 outline_result，跑了数分钟的成果确定性丢失。
                try:
                    await _checkpoint_partial_outline(
                        task_id,
                        _partial_holder.get("outline") or [],
                        _partial_holder.get("failed_chapters") or [],
                    )
                except asyncio.CancelledError:
                    # 任务被取消时该 await 也可能被取消 —— 必须让位给下面的
                    # finish_task，否则任务会永远停在 running（_tasks 泄漏）
                    logger.warning("断线兜底保存目录成果被取消（task=%s）", task_id)
                except Exception:
                    logger.warning("断线兜底保存目录成果失败（task=%s）", task_id, exc_info=True)
                await finish_task(task_id, "stopped", "客户端断开，任务已终止")

    # ✅ 进度增强：审核/修复期间 event_stream 整体挂起（最长 180s），
    #    由心跳通道以 stats_provider 周期性带出阶段与进度（ping 事件）。
    return StreamingResponse(with_heartbeat(event_stream(), stats_provider=_stats_provider),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- 正文生成 ----------
@router.post("/generate-content/{scheme_id}")
async def generate_content(scheme_id: str, request: Request, db=Depends(get_db)):
    # 解析请求体中的可选参数
    try:
        body = await request.json()
    except Exception:
        body = {}
    # ✅ 脏 JSON 防御：请求体可能是数组/字符串/null（脚本或代理误发），
    #    旧实现直接 body.get() → AttributeError → SSE 以非流式 500 失败，
    #    前端收不到 error 事件，只看到通用"SSE 请求失败"。统一降级为空选项。
    if not isinstance(body, dict):
        body = {}
    section_id = body.get("section_id")          # 指定章节，只生成该节点及子树
    mode = body.get("mode", "all")               # all / missing / section / continue
    word_budget_override = body.get("word_budget_override")  # 覆盖字数预算（整数）
    concurrency = body.get("concurrency")        # 并发档位：slow/balanced/fast 或整数
    # ✅ P0 修复（2026-09-19）：本次任务的有效并发 = 用户档位（归一为 1~5 整数）；
    #    未传档位时回退 AI 配置的目标档位（controller.target），而不是被自适应
    #    漂移污染的 controller.current。后续以**每任务独立信号量**严格执行。
    try:
        _conc_default = int(getattr(concurrency_controller, "target", 3) or 3)
    except (TypeError, ValueError):
        _conc_default = 3
    effective_concurrency = resolve_concurrency(
        concurrency, default=max(1, min(5, _conc_default)))
    # ✅ 布尔选项一律走 _coerce_bool（字符串/数字安全）：body.get(x, False)
    #    直接返回原值，前端若传 "false"（字符串）会被 Python 真值判为 True ——
    #    用户明明关了"超字数自动压缩"，却被白跑一轮 AI 压缩调用。
    force_rewrite = _coerce_bool(body.get("force_rewrite"), default=False)
    # ✅ 超字数自动压缩（可选，**默认关**）：生成后对「超过目标 130%（over）」的
    #    章节自动跑「AI 局部 replace/delete 压缩」，与手动「压缩本章」同一套实现
    #    （services/content_shrink.shrink_content_rounds）。
    #    默认关的原因：每章会额外产生最多 SHRINK_MAX_ROUNDS 次 AI 调用（费用），
    #    属产品决策项，必须由调用方显式开启。
    auto_shrink_over = _coerce_bool(
        body.get("auto_shrink_over"), default=False)

    # ✅ 自动流程：正文生成后自动执行全文一致性 Agent 修复
    #    （扫描 → 仲裁 → 定向修复 → 待用户在质量管理界面确认/回滚），默认开启。
    auto_consistency_repair = _coerce_bool(
        body.get("auto_consistency_repair"), default=True)
    # ✅ 一致性修复的「全文重写」强度开关（默认关）：关 = 只做定向修复，
    #    开 = 允许对冲突章节做整章重写（更彻底但 AI 调用与字数成本显著更高）。
    force_full_repair = _coerce_bool(
        body.get("force_full_repair"), default=False)
    consistency_severity = str(body.get("consistency_severity") or "high")
    if consistency_severity not in ("high", "medium", "low"):
        consistency_severity = "high"

    # F-CONTENT-STANDARD(2026-09-26): generation standard request params
    task_standard = normalize_standard(body.get("task_standard"))
    override_section_standard = _coerce_bool(
        body.get("override_section_standard"), default=False)

    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "方案不存在")
    scheme = dict(row)
    project_id = scheme["project_id"]

    # ✅ 进度增强（2026-09-15）：正文生成的实时进度状态。
    #    定义在 event_stream 之外，使心跳通道的 stats_provider 也能读到 ——
    #    单章 AI 调用期间 event_stream 整体挂起、无法 yield，只有心跳通道仍在
    #    运行，这是消除「进度条长时间静止」的关键。
    _prog: dict = {
        "started_at": time.monotonic(),
        "total": 0,
        "done": 0,        # 已处理章节数（含成功与失败）
        "failed": 0,
        "words": 0,       # 已落库正文累计字数
        "running": {},    # section_id -> {title, index, stage, stage_label, started_at, stage_started_at}
        "section_ms": [], # 每章实测耗时（秒），用于「平均单章耗时」统计
        "concurrency": 0,  # 并发档位（严格 = 用户选择，见 effective_concurrency）
        "phase": "prepare",
        "phase_label": "准备中",
        # ✅ 章节阶段起点：ETA 只按「章节生成阶段」的耗时外推，
        #    不含准备阶段（字数分配 AI 最长 180s）与全文一致性阶段。
        "sections_started_at": None,
        # ✅ 阶段耗时在线校准值（stage -> expect 秒），见 _calibrate_stage_expect
        "stage_expect": {},
        # ✅ 对外进度历史最大值（_monotonic_progress 护栏）
        "progress_max": 0.0,
    }
    # ✅ 并发档位严格 = 用户选择（每任务独立信号量，见 guarded_gen 的 _run_semaphore），
    #    不再读取全局自适应控制器的漂移值作展示 —— 展示值与实际闸门值是同一个数。
    _prog["concurrency"] = effective_concurrency

    def _stats_provider() -> dict:
        """心跳通道回调：返回运行统计快照（同步、绝不抛异常）。"""
        return _snapshot_stats(_prog)

    async def event_stream():
        task_id = await register_task("content_generation", project_id, scheme_id)
        # ✅ 断线兜底成果清单：客户端断开时 GeneratorExit 直接落在 yield 处（不经过
        #    任何 except），只有 finally 能收尾；而 finally 看不到 try 内部定义的
        #    done_ids / _failed_reasons。把清单提升到闭包外层容器，逐章维护，
        #    finally 才能把「本次生成了几章、哪几章失败」写进 checkpoint。
        _ckpt: dict = {"done": 0, "total": 0, "failed_sections": [], "words": 0}
        # ✅ 本次运行的质量计数（超字数章节数）：依据运行库日志，超字数是最高频的
        #    质量缺陷（96 章中 72% 判 over、均值 1.69 倍）——完成消息里必须让用户
        #    看到"本次有多少章超字数"，否则无从得知可以压缩。
        _stat: dict = {"over": 0}
        # F-CONTENT-STANDARD(2026-09-26 · B2): 生成标准校验汇总累加器。
        # 与 _stat 同级提升到闭包外层容器（早期失败路径也必须可读，不能 NameError），
        # 逐章在落库后累加，completed/stopped 终态一次性下发。
        _std_sum: dict = {"precise": 0, "fuzzy": 0, "issue_sections": 0,
                          "total_issues": 0, "errors": 0, "warnings": 0}
        # ✅ 修复内存泄漏：事件直接 yield，不再 subscribe 未消费的队列
        # ✅ E1 修复（2026-09-25 · 顺序护栏）：进度更新器必须在 try 之前建立。
        #    `if not leaves:`（无待生成章节）是 try 内的早退分支，它要调用
        #    _update_progress_safe；而该名字在闭包内被赋值 ⇒ Python 编译期即
        #    判定为**局部变量**，若定义写在 try 内靠后位置（章节树加载之后），
        #    早退分支取到的不是外层值而是直接 UnboundLocalError →
        #    被外层 except 吞成「正文生成失败」，而真实原因只是「本来就没有
        #    可生成的章节」。写锁同理必须与其成对提前建立。
        # ✅ B1（2026-09-23 · 早期失败二次崩溃）：收尾状态同样必须在 try
        #    **之前**就绪。旧实现把 total / done_ids / _failed_* / gen_runner
        #    定义在 try 内靠后位置，而 except / finally 收尾要读它们 ——
        #    早期异常（如章节树查询失败）时这些名字尚未绑定，收尾本身再抛
        #    NameError，任务永久卡在 running（前端显示"后台未运行"、无日志）。
        total = 0
        done_ids: set = set()
        _failed_ids: set = set()
        _failed_reasons: dict = {}
        gen_runner = None
        _ckpt: dict = {"done": 0, "total": 0, "failed_sections": [], "words": 0}

        def _content_ckpt_payload(event: str, message: str = "") -> dict:
            """正文生成终态 checkpoint 的**唯一**拼装点。

            唯一定义的意义：completed / stopped / error / 断线兜底 / 空章节
            五处收尾都要写同一套字段。逐处手写必然漂移 —— 历史上就出现过
            「completed 带 failed_count、error 不带」的口径矛盾，导致前端
            终态汇总与 stats 对不上。
            """
            return {
                'event': event,
                'message': message,
                'done': _ckpt.get("done", len(done_ids)),
                'total': _ckpt.get("total", total),
                'failed_count': len(_failed_ids),
                'failed_sections': list(_failed_reasons.values())[:50],
                'words': _ckpt.get("words", 0),
                # ✅ 本次运行统计（前端终态横幅 / 重挂接页展示）：
                #    run_words = 本次实际生成字数；over_count = 超字数章节数。
                'run_words': _ckpt.get("run_words", 0),
                'over_count': _ckpt.get("over_count", 0),
            }

        _db_write_lock = asyncio.Lock()
        _update_progress_safe = _locked_progress_updater(_db_write_lock, task_id)
        try:
            _prog["phase"], _prog["phase_label"] = "prepare", "准备中"
            _prog["progress_max"] = _monotonic_progress(_prog["progress_max"], 0.02)
            # ✅ 写锁口径统一：建立写锁之后的所有进度写入一律走
            #    _update_progress_safe（持锁 + 失败静默），与章节落库事务互斥。
            #    直调 update_progress 会与并发章节写事务争 SQLite 写锁。
            await _update_progress_safe(_prog["progress_max"], "正在加载章节树...",
                                        event="connecting")
            yield f"data: {json.dumps({'event':'connecting','task_id':task_id,'progress':_prog['progress_max'],'message':'正在加载章节树...'}, ensure_ascii=False)}\n\n"

            # 1) 先加载本方案所有章节（完整列表，用于构建上下文）
            cur = await db.execute(
                "SELECT * FROM sections WHERE scheme_id=? ORDER BY sort_order", (scheme_id,))
            all_sections = [dict(r) for r in await cur.fetchall()]

            # ✅ P0 修复（2026-09-19 · 目录顺序）：sort_order 是「同级内序号」而非全局
            #    序号，ORDER BY sort_order 会把不同层级的同序号节点排在一起（按"列"
            #    展开）。重排为目录树前序 DFS：生成任务排队、进度序号、前序同级参考
            #    均严格按目录层级递进，保证全文结构一致性。
            all_sections = order_sections_dfs(all_sections)

            # 2)+3) 目标叶子章节筛选（纯函数，规则与测试见 services/content_utils.py）
            leaves = select_target_leaves(
                all_sections, section_id=section_id, mode=mode,
                force_rewrite=force_rewrite)

            # 4) ✅ 字数设置（2026-09 新语义）：override = 二级章节目标字数
            #    - 二级章节（level<=2）为预算单元，其总字数严格受控
            #    - 单元内各叶子（三级等）由 AI 智能分配；AI 失败降级为均分
            #    - 不属于任何二级单元的叶子（自身即单元）直接使用 override
            if word_budget_override and word_budget_override > 0:
                by_id = {s["id"]: s for s in all_sections}

                def _find_unit(sec: dict):
                    """确定叶子的预算单元：叶子自身为二级时单元=自身；
                    否则向上找最近的 level<=2 祖先"""
                    if int(sec.get("level") or 1) <= 2:
                        return sec
                    pid = sec.get("parent_id", "")
                    while pid:
                        p = by_id.get(pid)
                        if not p:
                            return None
                        if int(p.get("level") or 1) <= 2:
                            return p
                        pid = p.get("parent_id", "")
                    return None

                units: dict[str, dict] = {}
                for leaf in leaves:
                    unit = _find_unit(leaf)
                    if unit is None:
                        unit = leaf  # 叶子自身就是二级/一级（无子树）→ 直接使用 override
                    u = units.setdefault(unit["id"], {"unit": unit, "leaves": []})
                    u["leaves"].append(leaf)

                # 构造 AI 分配输入
                units_payload = []
                for u in units.values():
                    unit, unit_leaves = u["unit"], u["leaves"]
                    if len(unit_leaves) == 1:
                        u["alloc"] = {unit_leaves[0]["id"]: word_budget_override}
                        continue
                    units_payload.append({
                        "unit_id": unit["id"],
                        "title": unit.get("title", ""),
                        "description": unit.get("description", ""),
                        "total_budget": word_budget_override,
                        "children": [
                            {"id": lf["id"], "title": lf.get("title", ""),
                             "description": lf.get("description", "")}
                            for lf in unit_leaves
                        ],
                    })

                # AI 智能分配（一次调用处理全部单元）；失败降级均分
                allocations: dict[str, int] = {}
                if units_payload:
                    try:
                        # ✅ 停止/暂停检查点：分配调用可能耗时数分钟且位于所有章生成之前
                        await wait_resume(task_id)
                        if is_stopped(task_id):
                            raise asyncio.CancelledError()
                        alloc_prompt = render(
                            "word_budget_allocate_system",
                            units_json=json.dumps(units_payload, ensure_ascii=False))
                        alloc_obj, _ = await collect_json_response(
                            [{"role": "system", "content": alloc_prompt},
                             {"role": "user", "content": "请输出字数分配结果。"}],
                            lambda o: [] if isinstance(o, dict) and "allocations" in o
                            else ["缺少 allocations 字段"],
                            timeout=OUTLINE_REQUEST_TIMEOUT,
                            # ✅ 场景归因：不打 scene 时 /ai/stats 的 by_scene
                            #    看不到本次字数分配调用量（该调用最长 180s，
                            #    是正文生成首段的隐藏耗时来源）。
                            json_mode=True, temperature=0.2,
                            scene="word_budget_alloc")
                        raw_alloc = alloc_obj.get("allocations", {})
                        if isinstance(raw_alloc, dict):
                            allocations = {
                                str(k): int(v) for k, v in raw_alloc.items()
                                if isinstance(v, (int, float)) and v > 0}
                    except Exception as e:
                        logger.warning("字数 AI 分配失败，降级均分: %s", e)

                    # ✅ 修复：遍历 units（含 leaves）写入 u["alloc"]。
                    #    原代码误遍历 units_payload（仅含 children、无 leaves）导致
                    #    KeyError；且分配结果写回的是临时 units_payload 而非 units，
                    #    后续 apply 阶段读取 u["alloc"] 再次 KeyError，使整个生成任务崩溃。
                    _apply_word_budget_allocations(units, word_budget_override, allocations)

                # ✅ 修复：应用分配 + 持久化移到 if units_payload 外部。
                # 旧实现仅在存在多叶子单元时才持久化，全单叶子单元（units_payload为空）时
                # word_budget 完全不写入 DB，导致 UI 展示与续写判断使用旧值/默认值。
                # 现无论 AI 分配成功还是降级均分，也无论是否有单叶子单元，都统一持久化。
                for u in units.values():
                    for lf in u["leaves"]:
                        lf["word_budget"] = u["alloc"].get(lf["id"],
                                                           word_budget_override)
                try:
                    # ✅ P1-1 性能优化：逐条 UPDATE → executemany 单次提交
                    await db.executemany(
                        "UPDATE sections SET word_budget=? WHERE id=?",
                        [(lf["word_budget"], lf["id"])
                         for u in units.values() for lf in u["leaves"]])
                    await db.commit()
                except Exception as e:
                    logger.warning("字数分配持久化失败（不影响生成）: %s", e)

                total_units = len(units)
                logger.info("字数预算：override=%d，%d 个二级预算单元，%d 个叶子",
                            word_budget_override, total_units, len(leaves))

            if not leaves:
                # ✅ E1：走 _update_progress_safe（与全文其余进度推送同口径：
                #    持写锁、失败静默、不阻断），且该更新器已在 try 之前建立
                #    —— 直接调 update_progress 会绕过写锁，与并发章节写事务
                #    争 SQLite 写锁（busy_timeout 内重试，拖慢收尾）。
                # ✅ 成果可恢复：空章节也是一次**正常的终态**，与 completed
                #    路径同口径先落 checkpoint 再置终态 —— 用户若在
                #    「没有待生成的章节」提示出现的瞬间刷新页面，重挂接
                #    GET /sse/task/{id} 才能读到 event=completed 而非"任务不存在"。
                try:
                    await _save_content_checkpoint(
                        task_id, _content_ckpt_payload("completed", "没有待生成的章节"))
                except Exception:
                    logger.warning("保存空章节 checkpoint 失败（task=%s）",
                                   task_id, exc_info=True)
                await _update_progress_safe(1.0, "没有待生成的章节", event="completed")
                yield f"data: {json.dumps({'event':'completed','task_id':task_id,'message':'没有待生成的章节'}, ensure_ascii=False)}\n\n"
                await finish_task(task_id, "completed")
                return

            # ✅ 模拟值闸门 + 按分组标题聚合：
            #   1. 未确认的模拟值（is_simulated=1 且未人工审核）与存在矛盾的事实
            #      禁止注入正文，避免把待裁决/编造值写成"确定性事实"；
            #   2. 按 group_title 聚合成小节，修复此前用单条事实名当小节标题
            #      导致同一分组事实各自成节、语义割裂的问题。
            # ✅ token 优化：结构化「项目关键事实」由共享函数统一构建（目录生成复用同一逻辑），
            #    每条事实截断 300 字、总量截断 6000 字（_build_facts_text 默认值）。
            #
            # ✅ BUG 修复（致命）：旧实现此处把事实文本按"当前叶子"构建（relevant_to=叶子），
            #    而该叶子变量只在上面 `word_budget_override` 分支的 `for leaf in leaves` 里绑定。
            #    用户使用默认字数（不传 word_budget_override，前端默认档）时该 for 不执行，
            #    `leaf` 未定义 → 此处抛 NameError → 被外层兜底 except 捕获，
            #    表现为「正文生成失败：name 'leaf' is not defined」，整批章节一章都生成不了。
            #    同时旧实现只在循环外算一次 facts_text，逐章精选实际只按最后一个叶子过滤，
            #    所有章节共享同一份事实（与「逐章精选」设计相悖）。
            #    现改为：事实行只加载一次，逐章在 _build_generation_context 内按 leaf 精选。
            facts_rows = await _load_facts_rows(db, scheme_id)
            # ✅ 知识库注入（§3.9）：项目级知识条目作为生成素材（非唯一数据源）。
            #    只加载一次，逐章在 _build_generation_context 内按 leaf 精选
            #    （旧实现全量注入每一章，长方案下整本制度库无差别重复发送）。
            knowledge_rows = await _load_knowledge_rows(db, scheme_id)

            def _knowledge_text_for(leaf: dict) -> str:
                if not knowledge_rows:
                    return ""
                from app.routers.knowledge import build_knowledge_text
                return build_knowledge_text(_filter_knowledge_rows(knowledge_rows, leaf))

            # ✅ BUG 修复（与目录生成同一处缺陷）：LIMIT 必须先过滤空值，
            #    否则未解析文档占满名额 → 已解析资料对正文生成不可见。
            from app.routers.global_facts import load_parsed_texts
            docs = await load_parsed_texts(db, project_id, limit=3)
            # ✅ 跨模块接线（2026-09-17 / 2026-09-23）：与目录生成对齐，优先消费
            #    bid_analysis 结构化提取成果（format_downstream_context 此前是
            #    死代码），提取失败/无成果时自动回退原文档摘录。
            # ✅ 预算 2000 → **4000**（2026-09-23 · 四项依据改造）：旧预算在
            #    18 项提取成果全量下发时**常态化截断**，与「完整调用、不丢失不
            #    截断」的约束相悖（后半段提取项整段消失，参数不可见）。
            raw_brief = "\n".join(docs)[:4000] if docs else ""
            project_brief = await _build_structured_brief(
                db, project_id, raw_brief, max_chars=4000)

            # ✅ 图表与正文一体生成：不再注入"预编排配图规划"。
            #    旧实现预查 chart_predictions(needed=1) 注入 {chart_plan}，是已删除的
            #    "正文生成前预编排"（Phase 2）的残留 —— 该表现在**只由正文同步登记**
            #    （register_inline_charts）写入，重新生成时会读到上一次的陈旧类型，
            #    既与"由 AI 自主判定是否配图、配哪种图"的设计相悖，也会把旧结论
            #    当成新规划误导模型。配图判定与插入位置现完全由正文 AI 自主完成
            #    （见 content_generation_system「图表同步生成规范」）。

            total = len(leaves)
            # ✅ 进度增强：登记总章节数与阶段（进入章节生成阶段）
            _prog["total"] = total
            _prog["phase"], _prog["phase_label"] = "sections", "生成正文"
            # ✅ ETA 口径修复：章节阶段起点独立记录 —— ETA 只按本阶段耗时外推，
            #    不含准备阶段（章节树加载 + 字数分配 AI 调用，最长 180s）与
            #    全文一致性阶段，否则开头几章的 ETA 会被系统性放大。
            _prog["sections_started_at"] = time.monotonic()
            _ckpt["total"] = total

            # 并发闸门（P0 修复 2026-09-19）：**每任务独立信号量**，容量严格 =
            #    用户前端档位（effective_concurrency，请求入口已归一）。
            #    不再复用全局 concurrency_controller.semaphore —— 旧实现三个缺陷：
            #      1) 正文/目录/多方案任务共用一把信号量，后启动任务的档位会
            #         挤掉先启动任务的用户设置（跨任务互染）；
            #      2) provider 层 adjust_concurrency 自适应降档使**实际并发与用户
            #         选择脱节**（需求要求严格遵循用户档位）；
            #      3) AI 配置保存/启动应用的 set_concurrency 会中途改变运行中任务闸门。
            #    全局控制器仍供 provider 层记录统计/熔断与事实提取等路径使用，
            #    只是不再充当正文生成的闸门。
            _run_semaphore = asyncio.Semaphore(effective_concurrency)

            # ✅ 追踪：已完成章节 ID 集合（done_ids 已在 try 之前预初始化，
            #    此处不得重复绑定 —— 重复绑定会让 finally 读到 try 内的新集合，
            #    早期 except 已登记的失败记录被丢弃）
            # ✅ 增强：本次生成过程中已完成章节的最新正文（落库清洗后的最终内容）。
            #    供后生成的同级章节做"前序风格参考"——旧实现只读启动时的 DB 快照，
            #    同批新生成时快照为空，摘要恒为空、功能失效；这里改为实时维护。
            generated_contents: dict[str, str] = {}

            # 预构建 parent_id → children 映射（避免 O(N²) 查找）
            by_parent: dict[str, list[dict]] = {}
            for s in all_sections:
                by_parent.setdefault(s.get("parent_id", ""), []).append(s)
            for k in by_parent:
                by_parent[k].sort(key=lambda x: x.get("sort_order", 0))
            nodes_map: dict[str, dict] = {s["id"]: s for s in all_sections}

            # ✅ E1：_db_write_lock / _update_progress_safe 已提升到 try 之前
            #    建立（见上方注释），此处不得重复定义 —— 迟到的赋值会让
            #    `if not leaves:` 早退分支抛 UnboundLocalError。

            # ---------- 进度推送辅助（2026-09-15 增强） ----------

            async def _push_stats():
                """推送一次运行统计（不写 DB；失败静默，不影响生成）。"""
                try:
                    await update_task_stats(task_id, **_snapshot_stats(_prog))
                except Exception:
                    pass

            def _progress_now() -> float:
                """折算当前加权进度并过「不可回退」护栏（返回并记录历史最大值）。"""
                p = _monotonic_progress(_prog.get("progress_max", 0.0),
                                        _weighted_progress(_prog))
                _prog["progress_max"] = p
                return p

            async def _on_stage(leaf_id: str, stage: str, detail: str = ""):
                """章节阶段切换：更新进度状态 + 推送 section_stage 事件。

                同时刷新加权总进度，使进度条在单章生成期间也持续前进，
                而不是等整章落库才跳一格。

                ✅ 阶段耗时在线校准：离开上一阶段时，用该阶段的实测耗时校准
                `stage_expect`（EMA），使后续阶段的填充速率贴合真实模型速度
                （报告遗留建议 #1）。只在切换点更新 ⇒ 阶段内曲线连续、
                进度不回退（_calibrate_stage_expect 内有完整说明）。
                """
                rec = _prog["running"].get(leaf_id)
                now = time.monotonic()
                if rec is not None:
                    prev_stage = rec.get("stage")
                    prev_started = rec.get("stage_started_at")
                    if prev_stage and prev_stage != stage and prev_started:
                        _calibrate_stage_expect(_prog, prev_stage, now - prev_started)
                    rec["stage"] = stage
                    rec["stage_label"] = _STAGE_LABELS.get(stage, stage)
                    rec["stage_started_at"] = now
                p = _progress_now()
                label = _STAGE_LABELS.get(stage, stage)
                title = (rec or {}).get("title", "")
                await _update_progress_safe(p, f"「{title}」{label}{detail}")
                await event_queue.put("data: " + json.dumps({
                    "event": "section_stage", "task_id": task_id,
                    "section_id": leaf_id, "title": title,
                    "index": (rec or {}).get("index", 0),
                    "total": _prog["total"], "stage": stage,
                    "stage_label": label, "detail": detail,
                    "progress": p,
                    "elapsed_ms": int((now - rec["started_at"]) * 1000) if rec else 0,
                }, ensure_ascii=False) + "\n\n")

            async def _persist_section(section_id: str, content: str,
                                        word_budget: int,
                                        eff_standard: str = "",
                                        fact_rows_for_section=None,
                                        ) -> tuple[str, int, str, dict]:
                """章节完成后立即持久化到数据库（不复用 buffer，避免崩溃全丢）。

                ✅ BUG 修复：内联图表登记会改写正文（修复成功替换代码 / 修复失败删块），
                word_count 与 word_status 必须基于【最终落库正文】计算；
                旧实现用清洗前的长度，导致库里字数、SSE 事件字数与实际正文偏大。

                Returns:
                    (最终正文, word_count, word_status, 生成标准校验报告)

                ✅ 事务守卫（BUG 修复 · 2026-09-16）：整个写块（清洗 → 图表全量重登记
                → 正文 UPDATE）必须原子。旧实现只对 `register_inline_charts` 的异常
                做 rollback；若**正文 UPDATE / commit 自身失败**（连接被中断、
                磁盘满、并发写冲突等），register 内已执行的
                `DELETE FROM chart_predictions WHERE section_id=?` 会被下一次 commit
                （例如 _mark_section_failed）顺带提交 —— 形成「正文里还有图表块、
                图表清单却没有登记」的不一致，导出预检与图表清单从此对不上。
                现在任何一步失败都整体回滚并原样抛出，交由调用方按"生成失败"处理。

                ✅ P1-3（2026-09-17 · 写锁临界区收缩）：正文清洗、标准审计、内联图表
                「计数 + 扫描 + 校验/修复 + 限额裁剪」全部是 CPU/读操作，已**整体前移到
                写锁之外**；锁内只保留最小事务（DELETE + INSERT 图表登记 + 正文 UPDATE
                + commit）。旧实现持锁做完整套计算，高并发档（5 章并行）时所有章节都在
                落库点排队等同一把全局写锁，实际并发退化为串行。
                事务原子性不变：图表登记与正文 UPDATE 仍在同一事务内提交。
                """
                # ---------- 锁外：CPU 密集（清洗 / 审计 / 图表扫描校验修复） ----------
                # ✅ 交付前清洗：消除 AI 生成正文中的口语化、宣传腔与 AI 表述，
                #    跳过 ``` 围栏内的图表代码块，不改变技术含义与数据。
                try:
                    _before = len(content)
                    content = sanitize_ai_content(content)
                    if len(content) != _before:
                        logger.debug("章节 %s 正文清洗：%d → %d 字",
                                     section_id[:8], _before, len(content))
                except Exception as e:
                    logger.warning("章节 %s 正文清洗失败（使用原文）: %s", section_id[:8], e)
                # ✅ 未闭合围栏补齐（2026-09-23）：AI 达到 max_tokens 上限时
                #    会把 ```mermaid 的收尾围栏截断，落库后**该行之后的所有正文
                #    都会被 Markdown 当成代码块内容吞掉**（导出 DOCX 时整章只剩
                #    一段代码）。必须在落库前补齐；修复本身失败只降级不阻断。
                try:
                    content = auto_fix_unclosed_fences(content)
                except Exception as e:
                    logger.warning("章节 %s 未闭合围栏修复失败（使用原文）: %s",
                                   section_id[:8], e)
                # ✅ 生成后审计：命中已废止标准编号时告警，供人工复核
                try:
                    _issues = quality_issues(content)
                    if _issues["abolished_standards"]:
                        logger.warning(
                            "章节 %s 疑似引用已废止标准：%s",
                            section_id[:8], _issues["abolished_standards"])
                except Exception:
                    pass
                # ✅ 内联图表（锁外计算）：修复成功用修复后代码写正文，修复失败删除坏块，
                #    保证导出质量；enforce_limits=True = 程序级配图上限（每章≤1、
                #    同类型全方案限额，对齐 OpenBidKit 编排 Agent 的"程序拍板"）。
                _chart_ok = True
                _chart_rows: list[tuple] = []
                try:
                    _type_counts = await _load_scheme_type_counts(
                        db, scheme_id, section_id)
                    content, _chart_rows = build_inline_chart_plan(
                        scheme_id, section_id, content,
                        enforce_limits=True, scheme_type_counts=_type_counts)
                except Exception as e:
                    # 图表登记失败：不写图表登记、继续落库正文（宁可少登记，不可坏正文）
                    logger.warning("章节 %s 内联图表登记失败（本次跳过图表登记，不影响正文）: %s",
                                   section_id[:8], e)
                    _chart_ok = False
                    _chart_rows = []

                # ✅ 正文字数剔除图表代码块：图表现与正文一体生成，若把内嵌的
                #    mermaid/chart-json 代码计入字数，会虚高字数并掩盖"正文偏短"。
                wc = text_word_count(content)
                ws = word_status_for(wc, word_budget)

                # F-CONTENT-STANDARD: 生成标准校验（**锁外**执行）
                # ✅ 缺口修复（2026-09-26 · B4）：此前 `standard_report()` 被直接写在
                #    `async with _db_write_lock` 内的 UPDATE 参数里 —— 全章正文 ×
                #    相关事实行的正则扫描是纯 CPU 密集操作，落在全局写锁里会与本函数
                #    上方 P1-3 的设计结论（「锁内只保留最小事务」）自相矛盾：
                #    并发档（5 章并行）时每章都要排队等同一把锁，实际并发退化为串行。
                #    口径等价性：锁内 `apply_inline_chart_plan` 只会**删除超限图表代码块**，
                #    而校验器第一步即整体剔除 ``` 围栏代码块，故「裁剪前/裁剪后」求值
                #    完全一致，移出锁不改变任何报告结果。
                std_for_report = eff_standard or PRECISE
                report: dict = {}
                report_json = ""
                if eff_standard or fact_rows_for_section:
                    try:
                        report = standard_report(
                            content, std_for_report, fact_rows_for_section or [])
                        report_json = json.dumps(report, ensure_ascii=False)
                    except Exception:
                        # 校验器异常绝不影响正文落库（降级为空报告形态 + WARNING）
                        logger.warning("章节 %s 生成标准校验失败（已降级，不影响正文落库）: %s",
                                       section_id[:8], exc_info=True)
                        report = {}
                        report_json = ""

                # ---------- 锁内：最小事务（图表重登记 + 正文 UPDATE） ----------
                async with _db_write_lock:
                    try:
                        if _chart_ok:
                            # ✅ B2 配套：锁内复核超限时同步裁剪正文（幽灵图防护）
                            content = await apply_inline_chart_plan(
                                db, section_id, _chart_rows, content)
                            # ✅ 字数必须按【裁剪后】正文重算：apply_inline_chart_plan
                            #    会删掉超出配图上限的图表块，块内文字随之消失。
                            #    旧实现沿用裁剪前的 wc/ws → 库里字数、SSE 载荷、
                            #    超字数统计三处口径漂移（front-end 显示的字数与
                            #    实际正文对不上）。
                            wc = text_word_count(content)
                            ws = word_status_for(wc, word_budget)
                        await db.execute(
                            "UPDATE sections SET content=?, word_count=?, word_status=?, "
                            "status='generated', updated_at=?, "
                            "last_generation_standard=?, last_generation_report=? "
                            "WHERE id=?",
                            (content, wc, ws, datetime.now().isoformat(),
                             # F-CONTENT-STANDARD: persist generation standard + report
                             eff_standard or "",
                             report_json,
                             section_id))
                        await db.commit()
                    except Exception:
                        # ✅ 整体回滚：绝不让半途事务（含已删图表登记）被后续 commit 提交
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        raise
                done_ids.add(section_id)
                generated_contents[section_id] = content
                # ✅ 进度增强：累计已落库正文字数（心跳统计展示用）
                _prog["words"] = _prog.get("words", 0) + (wc or 0)
                # ✅ 断线兜底清单：成功落库的章节数 / 已生成字数（finally 可读）
                _ckpt["done"] = _ckpt.get("done", 0) + 1
                _ckpt["words"] = _prog["words"]
                # ✅ 本次运行超字数章节计数（完成消息与 SSE 载荷使用）
                if ws == "over":
                    _stat["over"] = _stat.get("over", 0) + 1
                return content, wc, ws, report

            # ✅ B1：_failed_ids / _failed_reasons 已在 try 之前预初始化
            #    （早期 except/finally 收尾要读它们），此处不得重复绑定。

            def _count_failed(section_id: str, title: str = "", reason: str = ""):
                """登记一次章节失败（**只计数，不改库状态**）。

                ✅ BUG 修复：旧实现把"计数"和"改库状态"绑在一起（只在
                `_mark_section_failed` 内累加），导致两类不一致：
                  1) 强制重写时章节本有旧正文，失败后**不得**把状态打成 failed
                     （否则 missing 模式要求 content 为空才重选、普通模式又跳过非空
                     章节 → 该章从此无法被任何模式选中，只能手改）；既然不改库，
                     旧实现也就漏计了失败数 → 运行中 stats.failed 与终态
                     failed_count（done_ids 口径）自相矛盾；
                  2) 同一章可能被多条路径标记，需去重（用 _failed_ids 保证只计一次）。
                现拆开：本函数只管计数与明细，状态标记由 _mark_section_failed 决定。
                """
                if section_id in _failed_ids:
                    return
                _failed_ids.add(section_id)
                _failed_reasons[section_id] = {
                    "section_id": section_id, "title": title or "", "reason": reason or ""}
                _prog["failed"] = _prog.get("failed", 0) + 1
                # ✅ 断线兜底清单：失败明细（finally 可读，上限与 completed 载荷一致）
                _ckpt["failed_sections"] = list(_failed_reasons.values())[:50]

            async def _mark_section_failed(section_id: str, title: str = "",
                                           reason: str = "", *, mark_db: bool = True):
                """登记章节失败：计数总在跑，DB 状态标记按 mark_db 决定。

                mark_db=False 用于「章节本有旧正文」的失败（强制重写 / 续写场景）：
                保留其原有状态（generated/reviewed…），只计入失败统计。
                """
                _count_failed(section_id, title, reason)
                if not mark_db:
                    return
                async with _db_write_lock:
                    await db.execute(
                        "UPDATE sections SET status='failed', updated_at=? WHERE id=?",
                        (datetime.now().isoformat(), section_id))
                    await db.commit()

            async def _build_generation_context(leaf: dict,
                                                eff_standard: str = ""
                                                ) -> tuple[str, list, list, str]:
                """子函数1：构建章节生成的 AI 上下文（system prompt + user content）。
                返回 (生效标准, 本章相关事实行, messages, user_content)，
                user_content 供续写阶段复用。

                ✅ 2026-09-26（B2）：生效标准改由调用方（章节循环）预先解析并经
                `eff_standard` 传入，保证 section_start / 提示词 / 校验报告
                三处同源，避免事件字段与实际提示词口径漂移。

                Args:
                    leaf: 叶子章节 dict。
                    eff_standard: 调用方已解析好的生效标准；留空时内部兜底解析
                        （保持旧调用点可用，但会导致事件字段缺失 → 应显式传入）。
                """
                if not eff_standard:
                    eff_standard = resolve_effective_standard(
                        task_standard=task_standard,
                        override=override_section_standard,
                        section_standard=leaf.get("generation_standard", "") or "",
                        scheme_standard=scheme.get("generation_standard", "") or "",
                    )
                # ✅ 清理（2026-09-16 · ruff F841）：此处的 `leaf_id = leaf["id"]`
                #    是死赋值（下方一律用 leaf["id"]），删除以免与新代码混淆。
                parent_chain = _build_parent_chain(all_sections, leaf["id"], nodes_map, by_parent)
                # ✅ 增强：上级章节的标题 + 描述（旧实现只给标题链，AI 难以判断父级职责边界）
                parent_points: list[str] = []
                _pid = leaf.get("parent_id", "")
                while _pid and _pid in nodes_map:
                    _p = nodes_map[_pid]
                    _pd = (_p.get("description") or "").strip()
                    if _pd:
                        parent_points.append(f"- {_p.get('title', '')}：{_pd}")
                    _pid = _p.get("parent_id", "")
                parent_points.reverse()
                # ✅ 增强：同级章节标题 + 描述，以及前序同级正文摘要
                #    （摘要优先取本次生成过程中已完成的实时正文，使"风格延续"真正生效）
                sibling_lines, prev_sibling_summary = build_sibling_context(
                    all_sections, leaf, generated_contents=generated_contents,
                    children_by_parent=by_parent)

                word_budget = leaf_word_budget(leaf)
                # ✅ 逐章精选：按本章标题/描述从已加载事实行中预筛相关事实
                #    （旧实现只在生成循环外算一次，导致所有章节共享同一份事实）
                facts_text = _render_facts_text(facts_rows, relevant_to=leaf)
                # ✅ 编制依据注入：按方案类型 + 本章标题命中现行有效标准清单，
                #    禁止 AI 引用已废止版本或杜撰标准编号（见 standards_registry）
                standards_text = get_standards_text(
                    scheme.get("name", ""), scheme.get("type", ""),
                    section_title=leaf.get("title", ""))
                sys_prompt = render("content_generation_system",
                    section_number=_section_outline_number(leaf),
                    standards_text=standards_text,
                    scheme_name=scheme.get("name", ""),
                    scheme_type=scheme.get("type", ""))

                # F-CONTENT-STANDARD: 生成标准段落已由调用方解析（eff_standard），
                # 此处只负责渲染（DB 定制模板无占位符时也能生效）。
                sys_prompt += "\n\n" + build_system_block(eff_standard)

                user_content = f"【方案名称】：{scheme.get('name','')}\n【方案类型】：{scheme.get('type','')}\n【项目概述】：{project_brief}\n"
                user_content += f"【上级章节链】：{parent_chain}\n"
                if parent_points:
                    user_content += "【上级章节要点】：\n" + "\n".join(parent_points) + "\n"
                # ✅ 同级章节：明确告知"本层还有哪些兄弟章节、各自负责什么"，主动避免重复
                user_content += ("【同级章节（请避免内容重复）】：\n" + sibling_lines + "\n"
                                 if sibling_lines else "【同级章节（请避免内容重复）】：（无）\n")
                user_content += f"【当前章节编号】：{_section_outline_number(leaf)}\n"
                user_content += f"【当前章节】：{leaf['title']} — {leaf.get('description','')}\n"
                # ✅ 字数口径修复（见 _word_budget_hint）：旧实现只写"目标 X 字"，
                #    模型把它当"下限"→ 实测 72% 章节超字数（均值 1.69X）。
                user_content += f"【目标字数】：{_word_budget_hint(word_budget) or f'{word_budget}字'}\n"
                if prev_sibling_summary:
                    user_content += f"【前序同级章节结尾参考（衔接风格，勿重复）】：{prev_sibling_summary}\n"
                if facts_text:
                    # F-CONTENT-STANDARD(2026-09-26 · B1): 事实块引导语随模式变化。
                    # 精准 = 必须逐项原样引用；模糊 = 方向参考、可概括但不得矛盾。
                    # （此前两模式硬编码同一句「必须直接引用」，与模糊模式 user 块
                    #   的「方向参考」自相矛盾 → 选项在关键注入点上形同虚设。）
                    # ✅ 分段标签（2026-09-24 · G5 前提）：必须以 `【标签】：`
                    #    形式输出 —— prompt_governance 的上下文预算分配器靠
                    #    `_SEG_HEAD_RE`（^【…】[:：]）切段识别优先级；写成
                    #    "项目知识库素材（…）：" 这类括号说明形式则识别不到，
                    #    该段被当作"无标签正文"丢到最低优先级，预算削减时
                    #    **全局事实/知识库会先被砍掉**。
                    user_content += ("\n【全局事实变量（唯一可信数据源）】：\n"
                                     + build_facts_header(eff_standard)
                                     + f"{facts_text}\n")
                knowledge_text = _knowledge_text_for(leaf)
                if knowledge_text:
                    user_content += (
                        "\n【项目知识库素材】：\n"
                        "（企业管理制度/工艺要点/既有素材，"
                        "与本项目相关的表述应遵循其口径，数据仍以上方全局事实为准）\n"
                        f"{knowledge_text}\n")

                # F-CONTENT-STANDARD: append user block before messages
                user_content += "\n\n" + build_user_block(eff_standard)

                messages = [{"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_content}]
                _sec_facts = _filter_facts_rows(facts_rows, leaf)
                # ✅ 提示词治理（G5 预算 / G6 注入防护，默认关闭）：
                #    两个封装在配置未开启时**逐字返回原文**，故此处接入
                #    不改变任何既有行为；开启后才削减超长上下文 / 加资料围栏。
                #    必须在 messages 组装**之后**做 —— 治理对象是最终 user 文本。
                user_content = _guard_external_material(user_content)
                user_content = _apply_prompt_context_budget(user_content)
                if messages:
                    _last = messages[-1]
                    if _last.get("role") == "user":
                        messages[-1] = {**_last, "content": user_content}
                return eff_standard, _sec_facts, messages, user_content

            async def _generate_first_draft(messages: list[dict], title: str,
                                            stage_cb=None,
                                            max_tokens: int | None = None,
                                            ) -> tuple[str, Exception | None]:
                """子函数2：调用 AI 生成首轮正文（含失败重试+指数退避）。
                返回 (result, last_err)，last_err 为 None 表示成功。

                ✅ 进度增强：每次尝试前回调 stage_cb 上报「AI 生成中」阶段，
                AI 调用期间由 _await_with_stats 周期性推送运行统计。
                ✅ P1-2（2026-09-17）：max_tokens 按目标字数折算，给"跑飞"设物理上限。
                """
                result = ""
                last_err: Exception | None = None
                for attempt in range(CONTENT_SECTION_RETRIES + 1):
                    try:
                        if stage_cb is not None:
                            await stage_cb(
                                "draft",
                                f"（第 {attempt + 1}/{CONTENT_SECTION_RETRIES + 1} 次）"
                                if CONTENT_SECTION_RETRIES else "")
                        result = await _await_with_stats(asyncio.wait_for(
                            chat_with_fallback(messages, timeout=CONTENT_REQUEST_TIMEOUT,
                                               max_tokens=max_tokens,
                                               scene="content_draft"),
                            timeout=CONTENT_TOTAL_TIMEOUT), _push_stats)
                        # ✅ 空结果按失败处理（兜底）：正常情况下空返回已在
                        #    chat_with_fallback/provider 层转为异常并降级重试，
                        #    这里防止未来新路径漏网 —— 空串一旦当成功 break，
                        #    章节会直接标失败且不享受重试。
                        if not (result or "").strip():
                            raise RuntimeError("所有候选 AI 提供商均返回空正文")
                        last_err = None
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        last_err = e
                        if attempt < CONTENT_SECTION_RETRIES:
                            err_s = str(e).lower()
                            base = (CONTENT_RATE_LIMIT_BACKOFF
                                    if ("429" in err_s or "rate" in err_s or "limit" in err_s)
                                    else CONTENT_RETRY_BACKOFF)
                            backoff = base * (2 ** attempt)
                            logger.warning(
                                "章节 %s 生成失败（第 %d/%d 次），%.0fs 后重试: %s",
                                title, attempt + 1, CONTENT_SECTION_RETRIES + 1, backoff, e)
                            await asyncio.sleep(backoff)
                return result, last_err

            async def _continue_if_needed(result: str, leaf: dict, user_content: str, title: str,
                                          min_passes: int = 0, stage_cb=None,
                                          eff_standard: str = "") -> tuple[str, bool]:
                """子函数3：字数不足时自动续写（含重试）。

                返回 (补充后的完整正文, cont_failed)：
                cont_failed = 续写过程中发生过**最终失败**（全部轮次都没产出可用内容）。
                调用方据此在 section_done 事件里带出 continue_failed，前端才能
                区分「本章字数达标」与「本章欠字数且补写失败」—— 两者 word_status
                都是 under，光看状态用户无从判断该不该手工补。

                min_passes：最少续写轮数（0=默认按字数门槛；continue 模式传 1，
                即使已达字数目标也强制补写一轮 —— 用户点「续写本章」的语义就是
                在现有正文基础上继续扩充）。

                eff_standard：生效生成标准。续写是**独立的 AI 调用**（system 换成
                `content_continue_system`），首轮 user 里的标准块在后续轮不再重发
                （token 优化，见 `_ctx_for_continue`），若不在此处补一行模式提醒，
                精准模式在续写轮就会退化成"凭记忆"、出现与全局事实不一致的数值
                —— 即「切换标准后策略未更新」的隐性缺口。
                """
                word_budget = leaf_word_budget(leaf)
                # ✅ 口径与落库一致：只数正文文字，不含内嵌图表代码块 ——
                #    否则一张几百字符的 mermaid 图会提前满足字数门槛而不续写。
                wc = text_word_count(result)
                # ✅ 修复：续写消息中的 user 消息不得为空串 —— 部分 provider
                #    （Anthropic 等）显式拒绝空 content，直接报 400 导致续写全失败。
                #    调用方（continue 模式）现已传入完整上下文，这里再兜一层最小上下文。
                _user_ctx = user_content.strip() or (
                    f"【方案名称】：{scheme.get('name','')}\n【方案类型】：{scheme.get('type','')}\n"
                    f"【当前章节】：{leaf.get('title','')} — {leaf.get('description','')}\n")
                continue_count = 0
                cont_failed = False
                while ((wc < word_budget * WORD_UNDER_RATIO or continue_count < min_passes)
                       and continue_count < CONTENT_CONTINUE_MAX_ROUNDS):
                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        break
                    # ✅ 进度增强：上报「续写扩充中」阶段（含轮次与当前字数），
                    #    让用户看到字数在增长，而不是只看到进度条不动。
                    if stage_cb is not None:
                        await stage_cb(
                            "continue",
                            f"（第 {continue_count + 1} 轮，当前 {wc}/{word_budget} 字）")
                    cont_prompt = render("content_continue_system",
                        scheme_name=scheme.get("name", ""),
                        scheme_type=scheme.get("type", ""),
                        standards_text=get_standards_text(
                            scheme.get("name", ""), scheme.get("type", ""),
                            section_title=leaf.get("title", "")))
                    cont_tail = _safe_tail(result, 2000)
                    # ✅ token 优化：首轮之后不再重发完整 _user_ctx（全局事实/知识库
                    # 每轮重复计费且稀释指令），只发章节定位 + 尾部前文。
                    _ctx_for_continue = (_user_ctx if continue_count == 0 else (
                        f"【方案名称】：{scheme.get('name','')}\n【方案类型】：{scheme.get('type','')}\n"
                        f"【当前章节】：{leaf.get('title','')} — {leaf.get('description','')}\n"))
                    # F-CONTENT-STANDARD(2026-09-26 · B3): 续写轮补一行模式提醒。
                    # 后续轮不再重发首轮 user（token 优化），若不补提醒，
                    # 精准/模糊模式在续写阶段会退化为「凭记忆」→ 标准切换后策略不一致。
                    _std_hint = build_continue_hint(eff_standard)
                    cont_messages = [
                        {"role": "system", "content": cont_prompt},
                        {"role": "user", "content": _ctx_for_continue},
                        {"role": "assistant", "content": f"（前文已省略，以下为正文结尾部分）\n{cont_tail}"},
                        {"role": "user", "content": (
                            f"当前字数{wc}，目标{word_budget}字，请继续补充。"
                            f"补充后总字数上限为 {int(word_budget * 1.1)} 字"
                            f"（不得超出，接近上限时自然收尾）。"
                            if wc < word_budget else
                            f"当前字数{wc}已达到目标字数，请仍以上文为基础继续补充实质性内容"
                            "（具体工序细节、控制要点、检验标准、安全注意事项等），"
                            f"但**补充后总字数不得超过 {int(word_budget * 1.1)} 字**，"
                            "不要重复前文，不要输出图表代码块。")
                            + _std_hint}]
                    cont = None
                    cont_last_err: Exception | None = None
                    # ✅ P1-1（2026-09-17）：续写**不做失败重试** —— 续写是"锦上添花"，
                    #    单次尝试失败即保留原正文；旧实现每轮还重试
                    #    CONTENT_SECTION_RETRIES 次，为补充内容付出双倍超时代价。
                    # ✅ P1-2：续写输出上限按「剩余待补字数」折算（不再用配置的 32768）。
                    _cont_max_tokens = max_tokens_for_budget(
                        word_budget, chars=max(1, int(word_budget * 1.1) - wc))
                    try:
                        # ✅ 口径统一（2026-09-16）：续写与首稿使用同一总超时。
                        #    旧实现此处写死 360s（对照首稿的 CONTENT_TOTAL_TIMEOUT
                        #    =660s）：降级链在续写路径被提前砍断 —— 同一模型链路下
                        #    "首稿能成、续写必超时"，用户看到"续写最终失败"且字数不达标。
                        cont = await _await_with_stats(asyncio.wait_for(
                            chat_with_fallback(cont_messages, timeout=CONTENT_REQUEST_TIMEOUT,
                                               max_tokens=_cont_max_tokens,
                                               scene="content_continue"),
                            timeout=CONTENT_TOTAL_TIMEOUT), _push_stats)
                        cont_last_err = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        cont_last_err = e
                    if cont is not None and cont.strip():
                        # ✅ 段落级去重：弱模型常把最后一段原样重写，直接拼接会产生重复段落
                        cleaned = _dedup_continuation(result, cont)
                        if cleaned and cleaned not in result[-3000:]:
                            result += "\n" + cleaned
                        else:
                            logger.info("章节 %s 续写内容与前文重复，已丢弃", title)
                        wc = text_word_count(result)
                        # ✅ 单章硬上限：续写最多把正文扩到目标的 2 倍，防止无限扩写
                        if wc >= word_budget * 2:
                            logger.info("章节 %s 达到单章字数硬上限（%d 字），停止续写", title, wc)
                            break
                    elif cont_last_err is not None:
                        # ✅ 记录续写最终失败（某一轮彻底失败）：与"续写成功但字数
                        #    仍不达标"区分开 —— 后者交给 word_status=under 表达。
                        cont_failed = True
                        logger.warning("章节 %s 续写最终失败: %s", title, cont_last_err)
                    continue_count += 1
                return result, cont_failed

            async def _persist_and_notify(leaf_id: str, result: str,
                                          leaf: dict,
                                          eff_standard: str = "",
                                          fact_rows_for_section=None,
                                          ) -> tuple[str, int, int, str, dict]:
                """子函数4：持久化到 DB（含内联图表清洗），
                返回 (最终正文, word_count, word_budget, word_status, 生成标准校验报告)。"""
                wb = leaf_word_budget(leaf)
                content, wc, ws, report = await _persist_section(
                    leaf_id, result, wb,
                    eff_standard=eff_standard,
                    fact_rows_for_section=fact_rows_for_section)
                return content, wc, wb, ws, report

            async def _auto_shrink_if_over(result: str, leaf: dict,
                                           stage_cb=None) -> str:
                """超字数自动压缩（可选）：仅当章节判定为 over 时才跑 AI 压缩。

                与手动端点共用 `shrink_content_rounds`（同一提示词条目、同一保护
                区间校验、同一轮次/收敛参数），避免"自动压缩"与"手动压缩"两套行为。
                任何失败都保留原正文（宁可超字数，不可损坏正文），只记日志。
                """
                wb = leaf_word_budget(leaf)
                # ✅ 门槛判定与 word_status='over' 同口径（module 级纯函数，可单测）
                if not _should_auto_shrink(result, wb):
                    return result

                # 进度上报沿用 persist 阶段的标签（不新增阶段：_STAGE_MODEL 的
                # base/span 是成型进度模型，加阶段需重新配平并影响既有刻度）
                async def _on_round(round_no: int, cur_wc: int):
                    if stage_cb is not None:
                        await stage_cb(
                            "persist",
                            f"（超字数压缩第 {round_no} 轮，当前 {cur_wc}/{wb} 字）")

                def _prompt_factory(round_no: int, cur_wc: int) -> str:
                    return render(
                        "content_shrink_system",
                        scheme_name=scheme.get("name", ""),
                        scheme_type=scheme.get("type", ""),
                        section_title=leaf.get("title", ""),
                        current_words=cur_wc, target_words=wb,
                        round_no=round_no, max_rounds=SHRINK_MAX_ROUNDS)

                async def _call_ai(sys_prompt: str, user_payload: str) -> str:
                    # ✅ 修复（2026-09-17）：压缩调用必须显式传 max_tokens，否则
                    #    _attempt_candidate 会回退到配置值（可能 32768），与首稿/续写
                    #    已按 max_tokens_for_budget 折算的口径不一致，也违反「max_tokens
                    #    不要留空」的设计约束。压缩产出是短 JSON，按目标字数折算即可。
                    _shrink_max_tokens = max_tokens_for_budget(wb)
                    return await _await_with_stats(asyncio.wait_for(
                        chat_with_fallback(
                            [{"role": "system", "content": sys_prompt},
                             {"role": "user", "content": user_payload}],
                            timeout=CONTENT_REQUEST_TIMEOUT,
                            max_tokens=_shrink_max_tokens,
                            scene="content_shrink"),
                        timeout=CONTENT_TOTAL_TIMEOUT), _push_stats)

                try:
                    shrink = await shrink_content_rounds(
                        result, word_budget=wb,
                        prompt_factory=_prompt_factory, call_ai=_call_ai,
                        on_round=_on_round)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("章节 %s 自动压缩失败（保留原正文）: %s",
                                   leaf.get("title", ""), e)
                    return result
                if shrink["applied"]:
                    logger.info("章节 %s 自动压缩：%d → %d 字（%d 轮，%s）",
                                leaf.get("title", ""), shrink["before"],
                                shrink["word_count"], shrink["rounds_used"],
                                shrink["stop_reason"])
                    return shrink["content"]
                logger.info("章节 %s 自动压缩未生效（%s），保留原正文",
                            leaf.get("title", ""), shrink["stop_reason"])
                return result

            async def gen_one(idx: int, leaf: dict):
                """生成单个章节：开始 → 构建上下文 → AI生成 → 续写 → 持久化 → 通知
                （编排器，具体逻辑拆分为 _build_generation_context / _generate_first_draft /
                  _continue_if_needed / _persist_and_notify 四个子函数）

                ✅ 进度增强：章节开始即登记为「进行中」，并在各阶段（构建上下文 /
                AI 生成 / 续写 / 落库）推送 section_stage 事件与加权进度。"""
                leaf_id = leaf["id"]
                title = leaf["title"]
                db_written = False

                # F-CONTENT-STANDARD(2026-09-26 · B2): 本章生效标准**在事件之前**解析一次
                # （章节级 → 方案级 → 任务级强覆盖 → 默认 precise），
                # 使 section_start 就能携带该字段；后续上下文构建/提示词注入/
                # 校验报告全部复用这一个值，杜绝「事件与实际生效值不一致」。
                _eff_std = resolve_effective_standard(
                    task_standard=task_standard,
                    override=override_section_standard,
                    section_standard=leaf.get("generation_standard", "") or "",
                    scheme_standard=scheme.get("generation_standard", "") or "",
                )

                # ✅ 登记为进行中章节（心跳统计与阶段事件的数据源）
                _prog["running"][leaf_id] = {
                    "title": title, "index": idx + 1,
                    "stage": "context", "stage_label": _STAGE_LABELS["context"],
                    "started_at": time.monotonic(),
                    "stage_started_at": time.monotonic(),
                }

                async def _stage_cb(stage: str, detail: str = ""):
                    await _on_stage(leaf_id, stage, detail)

                yield json.dumps({
                    "event": "section_start", "section_id": leaf_id,
                    "title": title, "index": idx + 1, "total": total,
                    "stage": "context", "stage_label": _STAGE_LABELS["context"],
                    "progress": _progress_now(),
                    # F-CONTENT-STANDARD(2026-09-26 · B2): 本章实际生效的生成标准
                    "generation_standard": _eff_std,
                }, ensure_ascii=False) + "\n\n"
                await _on_stage(leaf_id, "context")

                try:
                    await wait_resume(task_id)
                    if is_stopped(task_id):
                        # ✅ 停止不是失败（2026-09-16）：必须抛 CancelledError 交由
                        #    上层停止分支收尾，**不得** yield section_error。
                        #    旧实现 yield 后 return，前端把被停止打断的章节记成
                        #    「失败」日志，与 stopped 终态自相矛盾（用户明明点了
                        #    停止，日志区却一片红）。
                        raise asyncio.CancelledError()

                    if mode == "continue":
                        # ✅ 续写模式（对齐 OpenBidKit 任意点续写）：以已有正文为底稿，
                        #    跳过首轮生成，直接进入续写（至少补写 1 轮，即使已达字数目标）。
                        #    select_target_leaves 的 continue 分支已保证仅有正文章节入选。
                        result = (leaf.get("content") or "").strip()
                        if not result:
                            # ✅ B2（2026-09-23）：continue 模式下空正文**必须登记
                            #    失败**。旧实现只发 section_error 就 return，失败
                            #    章不进 stats.failed / 终态 failed_count ——
                            #    前端 stats 显示"0 失败"、终态汇总也对不上，
                            #    用户看不出这一章其实没写成。
                            _reason = "该章节暂无正文，无法续写（请先生成）"
                            yield json.dumps({
                                "event": "section_error", "section_id": leaf_id,
                                "title": title, "reason": _reason,
                            }, ensure_ascii=False) + "\n\n"
                            await _mark_section_failed(leaf_id, title, _reason)
                            return
                        # ✅ 修复：续写同样需要完整生成上下文（方案信息/上级要点/同级边界/
                        #    逐章精选事实/标准清单/目标字数）。旧实现传空 user_content，
                        #    续写消息里出现空 user 消息（部分 provider 直接 400 报错），
                        #    且续写缺少事实与同级信息，易与全文口径不一致。
                        _eff_std_c, _sec_facts, _, user_content = await _build_generation_context(
                            leaf, _eff_std)
                        result, cont_failed = await _continue_if_needed(
                            result, leaf, user_content, title, min_passes=1,
                            stage_cb=_stage_cb, eff_standard=_eff_std_c)
                    else:
                        # 子函数1：构建上下文
                        _eff_std2, _sec_facts, messages, user_content = await _build_generation_context(
                            leaf, _eff_std)

                        # ✅ P1-2（2026-09-17）：按本章目标字数折算输出上限，
                        #    避免落到配置值（实测 32768）导致模型无上限扩写。
                        _draft_max_tokens = max_tokens_for_budget(
                            leaf_word_budget(leaf))
                        # 子函数2：AI 首轮生成（含重试）
                        result, last_err = await _generate_first_draft(
                            messages, title, stage_cb=_stage_cb,
                            max_tokens=_draft_max_tokens)

                        if last_err is not None:
                            logger.error("章节 %s 生成最终失败: %s", title, last_err)
                            _reason = ("章节生成超时，请重试"
                                       if isinstance(last_err, asyncio.TimeoutError)
                                       else str(last_err)[:200])
                            yield json.dumps({
                                "event": "section_error", "section_id": leaf_id,
                                "title": title, "reason": _reason,
                            }, ensure_ascii=False) + "\n\n"
                            # ✅ BUG 修复（force_rewrite 永久卡死）：旧实现无条件标 failed。
                            # 强制重写时该章节本有旧正文，失败后状态被打成 failed，
                            # 且 missing 模式要求 content 为空才重选（旧文还在），
                            # 普通模式又跳过非空章节 —— 该章从此无法被任何生成模式选中，
                            # 只能手改。旧文仍在时保持其原状态（generated/reviewed…）。
                            # ✅ 二次修复（2026-09-16）：失败**必须计入统计**（mark_db=False
                            # 只跳过状态标记），否则运行中 stats.failed 与终态 failed_count
                            # 不一致 —— 前端进度卡显示 0 失败、完成提示却说"N 章失败"。
                            await _mark_section_failed(
                                leaf_id, title, _reason,
                                mark_db=not (leaf.get("content") or "").strip())
                            db_written = True
                            return

                        if not (result or "").strip():
                            logger.warning("章节 %s 返回空内容，标记为失败", title)
                            _reason = "AI 返回空内容，请重试"
                            yield json.dumps({
                                "event": "section_error", "section_id": leaf_id,
                                "title": title, "reason": _reason,
                            }, ensure_ascii=False) + "\n\n"
                            await _mark_section_failed(
                                leaf_id, title, _reason,
                                mark_db=not (leaf.get("content") or "").strip())
                            db_written = True
                            return

                        # 子函数3：字数不足续写
                        result, cont_failed = await _continue_if_needed(
                            result, leaf, user_content, title, stage_cb=_stage_cb,
                            eff_standard=_eff_std2)

                    # 子函数4：持久化
                    await _stage_cb("persist")
                    # ✅ 超字数自动压缩（可选，默认关）：在**落库前**压缩 ——
                    #    正文只写一次（清洗/图表登记/字数统计一次成型），避免
                    #    "先落库再改写" 产生两倍写库与中间态。
                    if auto_shrink_over:
                        result = await _auto_shrink_if_over(result, leaf, _stage_cb)
                    content, wc, wb, ws, _std_report = await _persist_and_notify(
                        leaf_id, result, leaf,
                        eff_standard=_eff_std,
                        fact_rows_for_section=_sec_facts)
                    db_written = True

                    # ✅ 修复（2026-09-17）：自动生成章节的质量审计结果（口语化/废止标准）
                    #    此前只在 _persist_section 里记一条日志，不随 section_done 下发，
                    #    前端质量面板对自动生成章节看不到任何告警。现随事件携带，与手工保存
                    #    （sections.py）口径一致，生成过程中质量面板即可实时更新。
                    try:
                        _section_issues = quality_issues(content)
                    except Exception:
                        _section_issues = {"colloquial_hits": [], "abolished_standards": []}

                    # F-CONTENT-STANDARD(2026-09-26 · B2): 复用落库时**同一份**校验报告，
                    # 不在本处重复扫描 —— 旧实现对同一章正文跑了两遍正则（一次落库、
                    # 一次组事件），高并发档白烧 CPU。落库阶段已做 try/except 降级，
                    # 校验器异常绝不阻断生成，事件侧只需兜一个空形态。
                    _std_rep = _std_report or {
                        "standard": _eff_std or PRECISE, "passed": True,
                        "error_count": 0, "warning_count": 0, "issues": [],
                    }

                    # F-CONTENT-STANDARD(2026-09-26 · B2): 逐章累加终态汇总。
                    # 只统计"本次落库成功"的章节（失败章无正文可校验，不计入）。
                    _std_sum[_eff_std] = _std_sum.get(_eff_std, 0) + 1
                    _se = int(_std_rep.get("error_count") or 0)
                    _sw = int(_std_rep.get("warning_count") or 0)
                    if _se or _sw:
                        _std_sum["issue_sections"] += 1
                    _std_sum["errors"] += _se
                    _std_sum["warnings"] += _sw
                    _std_sum["total_issues"] += _se + _sw

                    # ✅ 续写失败信号收口：判定统一走纯函数
                    #    _continue_failed_flag（与 WORD_UNDER_RATIO 同口径）——
                    #    「续写失败但字数已达标」不报，「未达标且补写失败」才报。
                    word_budget = wb
                    cont_failed = _continue_failed_flag(cont_failed, wc, word_budget)
                    yield json.dumps({
                        "event": "section_done", "section_id": leaf_id,
                        "title": title, "word_count": wc, "word_budget": wb,
                        "word_status": ws,
                        # ✅ BUG-4：续写失败信号随事件下发，前端日志区据此标注
                        #    "补写失败"，用户不必自己比对字数去猜。
                        "continue_failed": cont_failed,
                        # ✅ 增强：携带最终正文，前端可即时刷新"当前选中章节"预览，
                        #    无需等整批生成结束后的全量 load()（旧实现预览长时间停留在旧内容）
                        "content": content,
                        "quality_issues": _section_issues,
                        # F-CONTENT-STANDARD(2026-09-26 · B2): 本章生效标准 + 完整校验报告
                        # （含 issues 明细供前端 Popover 展示；扁平计数保留便于告警 Tag）
                        "generation_standard": _eff_std or "",
                        "standard_report": _std_rep,
                        "standard_passed": _std_rep.get("passed", True),
                        "standard_error_count": _std_rep.get("error_count", 0),
                        "standard_warning_count": _std_rep.get("warning_count", 0),
                    }, ensure_ascii=False) + "\n\n"

                except asyncio.CancelledError:
                    # ✅ 语义修正：用户主动停止≠章节生成失败。停止时未完成章节保持
                    # pending（不标 failed、不发 section_error），否则质量面板把用户
                    # 停止计入失败、missing 模式还会把用户故意跳过的章重新选中。
                    if not db_written and not is_stopped(task_id):
                        yield json.dumps({
                            "event": "section_error", "section_id": leaf_id,
                            "title": title, "reason": "任务已取消",
                        }, ensure_ascii=False) + "\n\n"
                        try:
                            await _mark_section_failed(
                                leaf_id, title, "任务已取消",
                                mark_db=not (leaf.get("content") or "").strip())
                        except Exception:
                            pass
                    raise

            # 统一事件队列：所有事件（章节级别 + 总进度 + 终态）都 push 到这里
            # ✅ BUG-3（2026-09-19 · 有界队列 + 背压）：event_queue 必须**有界**。
            #    旧实现用无界队列：客户端断开后生成器被 cancel，消费者消失，
            #    生产者（章节任务）仍无界往里塞 → 内存单调增长直到进程被 OOM kill
            #    （长方案 + 弱客户端场景必现）。maxsize=1000 + 全链路阻塞式
            #    `await put` 形成背压：队列满时生产者自然等，被 cancel 时等待被打断。
            event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
            DONE_SENTINEL = object()

            async def guarded_gen(idx: int, leaf: dict):
                """包装 gen_one，把 SSE 事件逐行塞入 queue"""
                # ✅ 暂停闸门前置到信号量**之前**（2026-09-17）：
                #    旧实现的顺序是「先抢 semaphore → gen_one 内才 await wait_resume」，
                #    暂停时已在信号量内的章节会**一直占住全局并发许可**（实测暂停期间
                #    semaphore 剩余许可恒为 0）——同一进程内其它方案的生成任务被饿死，
                #    且用户点暂停后这批许可永不释放。闸门前置后暂停不占用任何并发额度。
                await wait_resume(task_id)
                if is_stopped(task_id):
                    # 保持与本文件其它检查点一致的协作式停止语义（不记为章节失败）
                    raise asyncio.CancelledError()
                # ✅ 每任务独立信号量：容量严格 = 用户档位（不随全局自适应漂移）；
                #    协程按 leaves 目录序创建，asyncio.Semaphore 对等待者 FIFO 放行
                #    → 启动顺序即目录树层级顺序，递进式生成
                async with _run_semaphore:
                    try:
                        async for sse_line in gen_one(idx, leaf):
                            if not sse_line.startswith("data: "):
                                sse_line = f"data: {sse_line}"
                            await event_queue.put(sse_line)
                    except asyncio.CancelledError:
                        # gen_one 被取消 → 已经发过 section_error，这里直接传播
                        raise
                    except Exception as e:
                        # 其他未预期异常 → 发 section_error
                        leaf_id = leaf["id"]
                        title = leaf["title"]
                        _reason = f"生成异常: {str(e)[:150]}"
                        try:
                            await event_queue.put(json.dumps({
                                "event": "section_error", "section_id": leaf_id,
                                "title": title, "reason": _reason,
                            }, ensure_ascii=False) + "\n\n")
                            # ✅ BUG 修复（与 gen_one 的失败分支口径统一）：
                            #    章节本有旧正文时**不得**把状态打成 failed（否则该章
                            #    从此无法被任何生成模式选中），但必须计入失败统计，
                            #    否则 stats.failed 与终态 failed_count 不一致。
                            await _mark_section_failed(
                                leaf_id, title, _reason,
                                mark_db=not (leaf.get("content") or "").strip())
                        except Exception:
                            pass
                    finally:
                        # ✅ 进度增强：章节结束（成功 / 失败 / 取消）后移出「进行中」
                        #    集合，否则被取消或异常终止的章节会永久留在统计里。
                        _rec = _prog["running"].pop(leaf["id"], None)
                        if _rec:
                            # ✅ 记录实测单章耗时：用于「平均单章耗时」展示，
                            #    也是阶段耗时校准的旁证（ETA 与"还要多久"判断依据）。
                            _started = _rec.get("started_at")
                            if _started:
                                _prog.setdefault("section_ms", []).append(
                                    max(0.0, time.monotonic() - _started))

            tasks = [asyncio.create_task(guarded_gen(i, leaf))
                     for i, leaf in enumerate(leaves)]
            for t in tasks:
                register_child_task(task_id, t)

            completed_count = 0

            async def run_all_generations():
                """等待所有章节生成完成，期间不断推送总进度"""
                nonlocal completed_count
                try:
                    for coro in asyncio.as_completed(tasks):
                        try:
                            await coro
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.error("章节生成子任务异常: %s", e)
                        completed_count += 1
                        # ✅ 进度增强：更新已处理数 → 折算加权进度，并附 ETA /
                        #    累计字数 / 失败数等统计，前端无需再自行推算。
                        _prog["done"] = completed_count
                        p = _progress_now()
                        snap = _snapshot_stats(_prog)
                        msg = f"已完成 {completed_count}/{total}"
                        if snap.get("eta_ms"):
                            msg += f"，预计剩余 {_fmt_duration(snap['eta_ms'])}"
                        # ✅ 失败章节显式提示：失败数与 ETA 同级重要（用户据此决定
                        #    是否等结束再重试），不再只在终态汇总里出现。
                        if _prog.get("failed"):
                            msg += f"（{_prog['failed']} 章失败）"
                        await _update_progress_safe(p, msg)
                        await event_queue.put(f"data: {json.dumps({
                            'event': 'progress', 'task_id': task_id,
                            'progress': p, 'message': msg,
                            'done': completed_count, 'total': total,
                            'failed': _prog.get('failed', 0),
                            'words': _prog.get('words', 0),
                            'elapsed_ms': snap.get('elapsed_ms'),
                            'eta_ms': snap.get('eta_ms'),
                            'stats': snap,
                        }, ensure_ascii=False)}\n\n")
                except asyncio.CancelledError:
                    logger.info("正文生成被取消")
                finally:
                    # ✅ 哨兵同样阻塞投递：队列满时 await 形成背压，
                    #    消费侧 break 后不再 get，靠取消传播解开等待。
                    await event_queue.put(DONE_SENTINEL)

            gen_runner = asyncio.create_task(run_all_generations())

            # ------ event_stream 主循环：统一从 queue 读并 yield ------
            # ✅ 修复竞态：只靠 DONE_SENTINEL break，去掉 queue.empty() + gen_runner.done() 提前退出
            while True:
                line = await event_queue.get()
                if line is DONE_SENTINEL:
                    break
                yield line

            # 等待生成任务完全收尾
            await gen_runner

            # ✅ 用户主动停止：未完成章节保持 pending（不批量标 failed）。
            # 停止不是失败，标 failed 会污染质量统计并使 missing 模式误选这些章。
            stopped = is_stopped(task_id)
            if stopped:
                # ✅ BUG 修复（2026-09-16）：停止时把进度硬推到 1.0 会让人误以为
                #    "已完成"——前端进度条跳到 100%、断线重连后任务列表/活动中心
                #    读到的也是 100%，与实际"只生成了一半"完全相反。这里只上报
                #    **真实进度**（过不可回退护栏），与目录生成的停止路径口径一致。
                stop_progress = _progress_now()
                # ✅ 成果清单落库（断线/刷新后可恢复"生成了哪几章")
                _ckpt["words"] = _prog.get("words", 0)
                try:
                    await _save_content_checkpoint(
                        task_id, _content_ckpt_payload("stopped", "用户已停止"))
                except Exception:
                    logger.warning("保存正文生成 checkpoint 失败（task=%s）", task_id, exc_info=True)
                await _update_progress_safe(stop_progress, "用户已停止", event="stopped")
                await finish_task(task_id, "stopped", "用户已停止")
                # ✅ 统一收口：stopped 与 completed 同口径携带 failed_sections
                yield "data: " + json.dumps(_stopped_payload(
                    task_id, "用户已停止", progress=stop_progress,
                    failed_sections=list(_failed_reasons.values())[:50],
                    standard_summary=_std_sum), ensure_ascii=False) + "\n\n"
                return

            # ✅ 自动流程：全文一致性 Agent 修复阶段
            #    正文全部落库后执行「扫描 → 仲裁 → 定向修复」：
            #    修复前自动快照、修复结果先写正文但状态为 pending_confirm，
            #    用户可在「审核与预检 → 全文一致性」界面逐条确认或一键回滚。
            #    该阶段任何失败都不影响已生成正文。
            consistency_summary = None
            if auto_consistency_repair and not stopped and mode not in ("section", "continue"):
                cons_queue: asyncio.Queue = asyncio.Queue()
                CONS_DONE = object()

                async def _run_consistency_pipeline():
                    try:
                        from app.services import consistency_scanner as _scanner
                        from app.services import conflict_arbiter as _arbiter
                        from app.services import repair_agent as _repair_agent

                        def _put(event: str, **kw):
                            cons_queue.put_nowait(json.dumps(
                                {"event": event, "task_id": task_id, **kw},
                                ensure_ascii=False) + "\n\n")

                        async def _scan_cb(done: int, total: int, message: str):
                            # ✅ 进度增强：扫描阶段映射到 [_SECTION_PHASE_MAX, 0.98]
                            _p = _SECTION_PHASE_MAX + (0.98 - _SECTION_PHASE_MAX) * (
                                done / total if total else 0.0)
                            _p = round(min(_p, 0.98), 4)
                            _put("consistency_scan_progress",
                                 done=done, total=total, message=message, progress=_p)
                            try:
                                await _update_progress_safe(_p, message)
                            except Exception:
                                pass

                        async def _repair_cb(done: int, total: int, message: str):
                            # ✅ 进度增强：定向修复阶段映射到 [0.98, 1.0]
                            _p = 0.98 + (1.0 - 0.98) * (done / total if total else 0.0)
                            _p = round(min(_p, 1.0), 4)
                            _put("consistency_repair_progress",
                                 done=done, total=total, message=message, progress=_p)
                            try:
                                await _update_progress_safe(_p, message)
                            except Exception:
                                pass

                        scan_result = await _scanner.run_scan(
                            db,
                            scheme_id=scheme_id,
                            project_id=project_id,
                            scheme_name=scheme.get("name", ""),
                            scheme_type=scheme.get("type", ""),
                            progress_cb=_scan_cb,
                        )
                        if not scan_result.get("sections"):
                            _put("consistency_skipped", reason="方案尚无正文，跳过一致性检查")
                            return

                        conflicts = await _arbiter.arbitrate_conflicts(
                            db, scan_result["conflicts"],
                            facts=scan_result["contexts"].get("global_facts", ""),
                            design_docs=scan_result["contexts"].get("design_docs", ""),
                            standards=scan_result["contexts"].get("standards", ""),
                            project_requirements=scan_result["contexts"].get("project_docs", ""))

                        def _sev(c):
                            return c.get("severity", "medium")

                        summary = {
                            "scan_id": scan_result["scan_id"],
                            "total": len(conflicts),
                            "high": sum(1 for c in conflicts if _sev(c) == "high"),
                            "medium": sum(1 for c in conflicts if _sev(c) == "medium"),
                            "low": sum(1 for c in conflicts if _sev(c) == "low"),
                            "pending": sum(1 for c in conflicts if c.get("status") == "pending"),
                            "skipped": sum(1 for c in conflicts if c.get("status") == "skipped"),
                        }
                        _put("consistency_scan_done",
                             scanned_sections=scan_result["sections"],
                             summary=summary)

                        repair_result = await _repair_agent.run_repair(
                            db,
                            scheme_id=scheme_id,
                            scan_id=scan_result["scan_id"],
                            conflicts=conflicts,
                            mode="auto",
                            severity_threshold=consistency_severity,
                            contexts=scan_result.get("contexts"),
                            progress_cb=_repair_cb,
                        )
                        _put("consistency_repair_done",
                             scan_id=scan_result["scan_id"],
                             repair_id=repair_result.get("repair_id"),
                             snapshot_id=repair_result.get("snapshot_id"),
                             repaired=repair_result.get("repaired", 0),
                             failed=repair_result.get("failed", 0),
                             skipped=repair_result.get("skipped", 0),
                             summary=summary)
                    except asyncio.CancelledError:
                        # ✅ BUG-1（2026-09-19 · 停止不是失败）：取消必须**re-raise**。
                        #    旧实现只记日志就吞掉 —— 用户点停止（或「防僵尸替换」触发
                        #    request_control("stop")）后，cons_runner 正常返回，
                        #    一致性阶段被当作"完成"，主流程继续发 completed 事件并把
                        #    schemes.status 改成"已生成"—— 用户明明点了停止，
                        #    方案却显示生成完毕。
                        logger.info("全文一致性修复阶段被取消（向上传播）")
                        raise
                    except Exception as e:
                        logger.warning("全文一致性 Agent 修复失败（不影响正文）: %s", e)
                        try:
                            cons_queue.put_nowait(json.dumps(
                                {"event": "consistency_failed", "task_id": task_id,
                                 "reason": str(e)[:150]}, ensure_ascii=False) + "\n\n")
                        except Exception:
                            pass
                    finally:
                        await cons_queue.put(CONS_DONE)

                cons_runner = asyncio.create_task(_run_consistency_pipeline())
                register_child_task(task_id, cons_runner)
                # ✅ 进度增强：进入全文一致性阶段（进度从 _SECTION_PHASE_MAX 推进到 1.0）。
                #    旧实现此处写死 1.0，导致最后数分钟的扫描 + 定向修复期间进度条
                #    停在 100% 不动，用户误判卡死。
                _prog["phase"], _prog["phase_label"] = "consistency", "全文一致性检查"
                await _update_progress_safe(_SECTION_PHASE_MAX,
                                            "正文完成，正在执行全文一致性扫描...")
                yield f"data: {json.dumps({'event':'consistency_scan_start','task_id':task_id,'progress':_SECTION_PHASE_MAX}, ensure_ascii=False)}\n\n"
                while True:
                    line = await cons_queue.get()
                    if line is CONS_DONE:
                        break
                    # 捕获扫描/修复结果，随 completed 事件一并带给前端
                    try:
                        _obj = json.loads(line.strip())
                        if _obj.get("event") == "consistency_scan_done":
                            consistency_summary = _obj.get("summary")
                            _m = (f"全文一致性扫描完成：发现 {consistency_summary.get('total', 0)} 处"
                                  f"（高 {consistency_summary.get('high', 0)}），正在定向修复...")
                            await _update_progress_safe(0.98, _m)
                        elif _obj.get("event") == "consistency_repair_done":
                            consistency_summary = _obj.get("summary") or consistency_summary
                            if isinstance(consistency_summary, dict):
                                consistency_summary = {
                                    **consistency_summary,
                                    "repair_id": _obj.get("repair_id"),
                                    "snapshot_id": _obj.get("snapshot_id"),
                                    "repaired": _obj.get("repaired", 0),
                                    "failed": _obj.get("failed", 0),
                                    "skipped": _obj.get("skipped", 0),
                                }
                            _m = (f"全文一致性修复完成：已修复 {_obj.get('repaired', 0)} 处，"
                                  f"失败 {_obj.get('failed', 0)} 处，"
                                  f"待人工确认 {_obj.get('skipped', 0)} 处")
                            await _update_progress_safe(1.0, _m)
                    except Exception:
                        pass
                    yield f"data: {line}"
                await cons_runner

            # 更新方案总字数
            cur = await db.execute("SELECT COALESCE(SUM(word_count),0) FROM sections WHERE scheme_id=?", (scheme_id,))
            total_wc = (await cur.fetchone())[0]
            await db.execute("UPDATE schemes SET word_count=?, status='审核中', updated_at=? WHERE id=?",
                             (total_wc, datetime.now().isoformat(), scheme_id))
            await db.commit()

            # ✅ P0-1：正文生成后自动把成功章节标记为待审核（打通 review 数据链）
            #    旧实现 review_status 始终为空字符串，审核 Tab 看不到"待审核"章节；
            #    导出前预检也无从判断"有多少正文尚未过审就准备交付"。
            if done_ids:
                placeholders = ",".join("?" * len(done_ids))
                await db.execute(
                    f"UPDATE sections SET review_status='pending', updated_at=? WHERE id IN ({placeholders})",
                    (datetime.now().isoformat(), *done_ids))
                await db.commit()

            # ✅ 失败章节统计：done_ids 只记录"成功落库"的章节，其余即为失败
            failed_count = max(total - len(done_ids), 0)

            # ✅ 全章失败语义（2026-09-16 · 依据运行库真实日志）：
            #    历史记录里出现过「正文0字完成（55/55 章失败）」被标成 **completed**
            #    的任务 —— 一章都没写成，任务列表/活动中心却显示"完成"。这里与目录
            #    生成的 all_failed 口径对齐：全章失败时终态判 failed 并发 error 事件
            #    （携带失败明细，前端可直接逐章重试）。
            if total > 0 and not done_ids:
                _reason_txt = "；".join(dict.fromkeys(
                    (v.get("reason") or "")[:60]
                    for v in list(_failed_reasons.values())[:3]))
                _all_msg = (f"全部 {total} 章生成失败（AI 服务异常），请检查模型/AI 配置后重试"
                            + (f"：{_reason_txt}" if _reason_txt else ""))
                _ckpt["words"] = _prog.get("words", 0)
                try:
                    await _save_content_checkpoint(
                        task_id, _content_ckpt_payload("error", _all_msg[:200]))
                except Exception:
                    logger.warning("保存正文全章失败 checkpoint 失败（task=%s）",
                                   task_id, exc_info=True)
                await finish_task(task_id, "failed", _all_msg[:200])
                _err_payload = {'event': 'error', 'task_id': task_id,
                                'message': _all_msg,
                                'failed_count': failed_count, 'total': total}
                if _failed_reasons:
                    _err_payload['failed_sections'] = list(_failed_reasons.values())[:50]
                yield f"data: {json.dumps(_err_payload, ensure_ascii=False)}\n\n"
                return

            # ✅ 图表与正文同步生成：图表编排已在正文生成时由文本 AI 一体完成
            #    （配图判定 → Mermaid/JSON 生成 → 插入位置由正文 AI 判定，
            #     格式与正文相同；每章持久化时由 register_inline_charts
            #     提取、校验/修复并登记），不再有正文后置的独立图表管线。
            # ✅ 字数口径修复（依据运行库真实日志）：`total_wc` 是**方案合计**字数，
            #    旧实现无论本次生成多少章都报"正文 N 字完成"，用户重生成单章时会
            #    看到整个方案的字数，误以为"一章写了 7 万字"。现区分本次与合计。
            _run_words = _prog.get("words", 0)
            _partial_run = mode in ("section", "continue") or len(done_ids) < total
            if _partial_run:
                _done_msg = f"本次生成 {_run_words} 字（方案合计 {total_wc} 字）"
            else:
                _done_msg = f"正文{total_wc}字完成"
            if failed_count:
                _done_msg += f"（{failed_count}/{total} 章失败）"
            # ✅ 超字数提示（依据运行库日志：96 章中 72% 超字数、均值 1.69 倍）：
            #    把本次超字数的章节数写进完成消息，用户才知道可以点「压缩本章」
            #    或开启「超字数自动压缩」。
            _over_n = _stat.get("over", 0)
            if _over_n:
                _done_msg += f"（{_over_n} 章超字数，可压缩或开启自动压缩）"
            # ✅ 同步进 _ckpt：stopped / 断线兜底 / 服务端取消 等**非 completed**
            #    收尾路径也要带上「本次生成字数 / 超字数章数」——否则断线重挂后
            #    前端统计全为 0，与在线看到的横幅对不上（这些字段已入白名单）。
            _ckpt["run_words"] = _run_words
            _ckpt["over_count"] = _over_n
            completed_payload = {
                'event': 'completed', 'task_id': task_id,
                # ✅ 终态进度与计数必须随 completed 一起下发：前端断线重挂
                #    读到的最后一个 completed 事件若缺 progress/done/total，
                #    进度条会停在最后一章的中间值、章节计数也对不上总数。
                'progress': 1.0, 'done': total, 'total': total,
                'word_count': total_wc, 'failed_count': failed_count,
                'run_words': _run_words, 'over_count': _over_n,
                'message': _done_msg,
            }
            # ✅ 失败明细（2026-09-16 增强）：只给"失败 N 章"用户无法定位是哪几章、
            #    为什么失败（章节可能压根没进前端日志，例如异常路径）。
            #    随 completed 一并下发 section_id/标题/原因，前端可直接在日志区
            #    展示并支持逐章重试。上限 50 条，避免超大方案把载荷撑爆。
            if _failed_reasons:
                completed_payload['failed_sections'] = list(_failed_reasons.values())[:50]
            if consistency_summary is not None:
                completed_payload['consistency_summary'] = consistency_summary
            # F-CONTENT-STANDARD(2026-09-26 · B2): 生成标准校验汇总下发
            completed_payload['standard_summary'] = _std_sum
            # ✅ 收尾顺序（2026-09-16 修复）：先落 checkpoint → 再置终态 → 最后 yield。
            #    旧实现 `yield completed` 之后才 finish_task：前端收到 completed 立即
            #    break（SSE 生成器被 aclose），`finally` 兜底就把**已成功完成**的任务
            #    写成 stopped（"客户端断开，任务已终止"）—— 任务列表/活动中心与真实
            #    结果相反（与目录生成同一类缺陷，本轮统一口径）。
            _ckpt["words"] = _prog.get("words", 0)
            try:
                await _save_content_checkpoint(
                    task_id, _content_ckpt_payload("completed", _done_msg))
            except Exception:
                logger.warning("保存正文生成 checkpoint 失败（task=%s）", task_id, exc_info=True)
            await _update_progress_safe(1.0, _done_msg, event="completed")
            _prog["progress_max"] = 1.0
            await finish_task(task_id, "completed", _done_msg)
            yield f"data: {json.dumps(completed_payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # ✅ 成果清单落库（2026-09-21 修复）：本路径**必须**写 checkpoint。
            #    旧实现直接 finish_task + yield —— 用户在「全文一致性」阶段
            #    （常耗时数分钟）关闭页面时，已生成几十章的成果清单永久丢失，
            #    重挂接只看到一句"任务已取消"。与停止/断线路径同口径收口。
            #    收尾顺序仍为：checkpoint → finish_task → yield。
            try:
                await _save_content_checkpoint(
                    task_id, _content_ckpt_payload("stopped", "任务已取消"))
            except Exception:
                logger.warning("取消路径保存正文 checkpoint 失败（task=%s）",
                               task_id, exc_info=True)
            await finish_task(task_id, "stopped", "任务已取消")
            yield "data: " + json.dumps(_stopped_payload(
                task_id, "任务已取消",
                failed_sections=list(_failed_reasons.values())[:50]),
                ensure_ascii=False) + "\n\n"
        except Exception as e:
            logger.exception("正文生成失败")
            # ✅ 成果清单落库：整批失败时已落库的章节仍需可追溯（哪几章成功/失败）
            try:
                await _save_content_checkpoint(
                    task_id, _content_ckpt_payload("error", str(e)[:200]))
            except Exception:
                logger.warning("保存正文生成失败 checkpoint 失败（task=%s）", task_id, exc_info=True)
            await finish_task(task_id, "failed", str(e))
            yield f"data: {json.dumps({'event':'error','task_id':task_id,'message':str(e)}, ensure_ascii=False)}\n\n"
        finally:
            # ✅ 断开兜底：客户端断开时 GeneratorExit 直接落在 yield 处（不经过
            # except），gen_runner 无人 await、任务永远停在 running。幂等清理：
            # 仅当任务尚未进入终态时兜底 finish_task（其内部会对非 completed
            # 状态统一 cancel child_tasks，停止语义单点收口）。
            if has_active_task(task_id):
                # ✅ B1：早期异常时协程尚未创建，gen_runner 为 None ——
                #    无守卫直接 .cancel() 会抛 AttributeError，让 finally 自身
                #    失败 → finish_task 走不到 → 任务永久卡在 running。
                if gen_runner is not None:
                    gen_runner.cancel()
                # ✅ 成果清单落库（2026-09-16 增强）：把「已生成 N/M 章、哪几章失败」
                #    写进 checkpoint —— 用户刷新页面后前端重挂接 GET /sse/task/{id}
                #    即可拿到失败明细，而不是只有一句"后台任务已停止"。
                try:
                    await _save_content_checkpoint(
                        task_id, _content_ckpt_payload(
                            "stopped", "客户端断开，任务已终止"))
                except asyncio.CancelledError:
                    logger.warning("断线兜底保存正文成果被取消（task=%s）", task_id)
                except Exception:
                    logger.warning("断线兜底保存正文成果失败（task=%s）", task_id, exc_info=True)
                await finish_task(task_id, "stopped", "客户端断开，任务已终止")
            # ✅ P0 悬挂写事务兜底（2026-09-25 · 对齐 bid_analysis 的 shield 模式）：
            #    SSE 流被取消时，task_registry / checkpoint 的写可能停在
            #    「execute() 已 BEGIN、commit() 未发生」的状态。全局共享连接
            #    一旦挂着未提交写事务，**后续所有写都要等它提交/回滚** →
            #    全站 500 "database is locked"（db.settle_global_conn 的注释
            #    记载的正是这一事故）。本模块此前只 import 未调用。
            #    用 asyncio.shield：外层协程被取消时，内层回滚仍在后台跑完，
            #    否则「为解冻而去 await」本身会被取消 → 事务照旧悬挂。
            try:
                await asyncio.shield(settle_global_conn("content_finally"))
            except asyncio.CancelledError:
                logger.warning("正文生成收尾悬挂事务收敛被取消（task=%s）", task_id)
            except Exception:
                logger.warning("正文生成收尾悬挂事务收敛失败（task=%s，不阻断）",
                               task_id, exc_info=True)

    # ✅ 进度增强：心跳通道携带运行统计（stats_provider）—— 单章 AI 调用期间
    #    event_stream 整体挂起、无法 yield，只有心跳 Task 仍在运行，由它周期性
    #    推送「已耗时 / 进行中章节 / 累计字数 / ETA」，消除进度条长时间静止。
    return StreamingResponse(
        with_heartbeat(event_stream(), stats_provider=_stats_provider),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- 全局事实提取（增强版：分段 + 合并去重 + 矛盾检测） ----------
@router.post("/generate-facts/{scheme_id}")
async def generate_facts(scheme_id: str, request: Request, db=Depends(get_db)):
    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, "方案不存在")
    scheme = dict(row)
    project_id = scheme["project_id"]

    # ✅ 缺值模式（对齐 OpenBidKit 全局事实三模式）：
    # fabricate（默认，合理补全并标记模拟值）/ omit（不杜撰，剔除模拟值）/
    # placeholder（资料未给出的值置为【待填写】）
    try:
        _facts_body = await request.json()
    except Exception:
        _facts_body = {}
    missing_value_mode = str((_facts_body or {}).get("missing_value_mode")
                             or "fabricate").strip().lower()
    if missing_value_mode not in ("fabricate", "omit", "placeholder"):
        missing_value_mode = "fabricate"
    # ✅ 增量提取（2026-09-17）：默认跳过已完成段（上次提取成功的分段不再重复
    #    调用 AI）；force=true 时全量重提取（「全部重新提取」入口使用）。
    force_full = bool((_facts_body or {}).get("force"))

    async def event_stream():
        from app.services.facts_extractor import (
            run_extraction_pipeline, persist_extraction,
            format_for_frontend, invalidate_export_cache,
            load_completed_chunks, save_extracted_chunks,
        )
        task_id = await register_task("facts_generation", project_id, scheme_id)
        pipe_task: asyncio.Task | None = None
        # ✅ BUG 修复（进度回退）：事实提取链路的进度来源有多处——SSE 层
        #    （0.05 加载资料 / 0.08 分段就绪 / 0.09 增量提示）与管线内部
        #    （0.03 正在智能分段 / 0.06 截断告警），两侧口径不一致会发出
        #    比上一条更小的进度（实测 0.08 → 0.03），前端事实提取路径无
        #    单调护栏、直接 setProgress → 进度条肉眼可见「倒退」，观感等同
        #    卡死或任务被重置。目录/正文链路已有 _monotonic_progress 护栏，
        #    此处共用同一口径：所有进度事件统一抬到历史最大值，不可回退。
        _max_pushed = 0.0

        def _sse(obj: dict) -> str:
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        async def _stage(p: float, m: str) -> str:
            """更新任务进度并返回一条 progress SSE 事件（不可回退）"""
            nonlocal _max_pushed
            _max_pushed = p = _monotonic_progress(_max_pushed, p)
            await update_progress(task_id, p, m)
            return _sse({"event": "progress", "task_id": task_id,
                         "progress": p, "message": m})

        try:
            await wait_resume(task_id)
            if is_stopped(task_id):
                await finish_task(task_id, "stopped", "用户已停止")
                yield _sse({"event": "stopped", "task_id": task_id})
                return

            # 1. 加载项目资料
            yield await _stage(0.05, "正在加载项目资料...")

            cur = await db.execute(
                "SELECT file_name, parsed_markdown FROM project_documents "
                "WHERE project_id=? AND parsed_markdown IS NOT NULL "
                "AND parsed_markdown != '' ORDER BY created_at, id",
                (project_id,))
            docs = await cur.fetchall()
            # ✅ BUG 修复：进度消息"已加载 X 份资料"旧实现把【未解析文档】
            #    也计入（docs 含 parsed_markdown 为空的记录），虚报提取覆盖面。
            #    现仅统计有文本的文档；source_file_names 同步只含已解析文档
            #    （原先还把未解析文件名传给管线，而管线并未消费该参数）。
            parsed_docs = [(d[0], d[1]) for d in docs if d[1]]
            source_files = [d[0] for d in parsed_docs]
            all_text = "\n\n".join(f"=== {d[0]} ===\n{d[1]}" for d in parsed_docs)

            # ✅ 分步工作流：提取前置是"已解析文档"。
            # 无任何已解析文本时不再静默降级为方案基本信息（会产出全模拟值），
            # 而是明确提示用户先完成 上传保存 → 解析 两步。
            pending_docs = [d[0] for d in docs if not d[1]]
            if not all_text.strip():
                hint = ("项目下没有可提取的资料文本。请先在「全局事实」页上传文件并点击「解析文档」。"
                        if docs else
                        "项目下没有已上传的资料文档。请先在「全局事实」页上传文件并解析。")
                if pending_docs:
                    hint += f"（当前有 {len(pending_docs)} 份文档待解析）"
                await finish_task(task_id, "failed", hint)
                yield _sse({"event": "error", "task_id": task_id, "message": hint})
                return

            if pending_docs:
                yield _sse({
                    "event": "warning", "task_id": task_id,
                    "message": (f"有 {len(pending_docs)} 份文档尚未解析，"
                                f"本次提取仅使用已解析的 {len(docs) - len(pending_docs)} 份")})

            yield await _stage(
                0.08, f"已加载 {len(source_files)} 份资料（{len(all_text)} 字），准备分段...")

            # 2. 分段提取：pipeline 的分段级进度回调经队列桥接为实时 SSE，
            #    同时支持提取过程中的暂停/停止（旧实现只能等整段跑完才看得到结果）。
            q: asyncio.Queue = asyncio.Queue()

            async def _progress_cb(p: float, m: str):
                await q.put((p, m))

            async def _wait_resume_cb():
                await wait_resume(task_id)

            # ✅ 增量提取：加载已完成段指纹（force 时跳过读取 → 全量重提取）
            # ✅ BUG-1 修复：进度作用域含 scheme_id，避免多方案共享项目时
            #    非首方案命中其它方案的完成段指纹而 all_skipped 早退。
            completed_chunks = set() if force_full else \
                await load_completed_chunks(db, project_id, scheme_id)
            if completed_chunks:
                yield await _stage(
                    0.09, f"增量提取：已记录 {len(completed_chunks)} 段历史成果，"
                          "重复段落将自动跳过...")

            pipe_task = asyncio.create_task(run_extraction_pipeline(
                all_text,

                progress_cb=_progress_cb,
                should_stop=lambda: is_stopped(task_id),
                wait_resume_cb=_wait_resume_cb,
                missing_value_mode=missing_value_mode,
                completed_chunks=completed_chunks,
            ))

            while True:
                if pipe_task.done() and q.empty():
                    break
                if is_stopped(task_id):
                    pipe_task.cancel()
                    try:
                        await pipe_task
                    except BaseException:
                        pass
                    pipe_task = None
                    await finish_task(task_id, "stopped", "用户已停止")
                    yield _sse({"event": "stopped", "task_id": task_id})
                    return
                try:
                    p, m = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                yield await _stage(p, m)

            extraction_result = await pipe_task
            pipe_task = None

            # 3. 完整性保护：零事实时绝不覆盖既有事实
            #    （旧实现会先删库再插入 0 条 → 一次失败提取清空全部历史事实）
            # ✅ 增量提取例外（2026-09-17）：全部段都已提取过（无新增内容）时
            #    属正常成功——既有事实保持上次落库结果，进度照常记录。
            if extraction_result.all_skipped:
                done_msg = (f"全部 {extraction_result.skipped_chunks} 段均已提取过，"
                            "无新增内容需要提取；如需强制刷新请使用「全部重新提取」")
                await update_progress(task_id, 1.0, done_msg, event="completed")
                await save_extracted_chunks(
                    db, project_id, scheme_id,
                    extraction_result.chunk_hashes_ok,
                    extraction_result.chunk_hashes_all)
                await finish_task(task_id, "completed", done_msg)
                yield _sse({"event": "completed", "task_id": task_id,
                            "message": done_msg,
                            "skipped": extraction_result.skipped_chunks})
                return
            if not extraction_result.groups or extraction_result.total_items == 0:
                warn = "；".join(extraction_result.warnings) or "未提取到任何事实，已保留原有事实"
                await finish_task(task_id, "failed", warn)
                yield _sse({"event": "error", "task_id": task_id, "message": warn})
                return

            # 4. 保存并输出
            yield await _stage(0.96, "正在保存事实...")
            await persist_extraction(extraction_result, db, project_id, scheme_id)
            await invalidate_export_cache(db, scheme_id)
            # ✅ 增量提取：记录本次完成段指纹并清理失效残留（删除/重解析的文档）
            await save_extracted_chunks(
                db, project_id, scheme_id,
                extraction_result.chunk_hashes_ok,
                extraction_result.chunk_hashes_all)

            frontend_data = format_for_frontend(extraction_result)

            # 如果有告警，额外推送 warning 事件
            if extraction_result.warnings:
                yield _sse({"event": "warning", "task_id": task_id,
                            "warnings": extraction_result.warnings})

            # ✅ 部分失败时在任务消息里显式标注，页面刷新/断线重挂接后仍可见
            _seg = extraction_result.segment_stats or {}
            _failed_n = int(_seg.get("failed") or 0)
            _total_n = int(_seg.get("total") or 0)
            _skipped_n = int(_seg.get("skipped") or 0)
            _skip_part = f"，跳过 {_skipped_n} 段已提取" if _skipped_n else ""
            done_msg = (f"全局事实提取完成{_skip_part}" if not _failed_n
                        else f"全局事实提取完成（{_failed_n}/{_total_n} 段失败{_skip_part}，结果可能不完整）")
            await update_progress(task_id, 1.0, done_msg, event="completed")
            yield _sse({"event": "completed", "task_id": task_id,
                        "message": done_msg, **frontend_data})
            await finish_task(task_id, "completed", done_msg)
        except asyncio.CancelledError:
            await finish_task(task_id, "stopped", "用户已停止")
            yield _sse({"event": "stopped", "task_id": task_id})
        except Exception as e:
            logger.exception("全局事实提取失败")
            await finish_task(task_id, "failed", str(e))
            yield _sse({"event": "error", "task_id": task_id, "message": str(e)})
        finally:
            # 客户端断开/生成器关闭时，避免后台提取任务泄漏
            if pipe_task is not None and not pipe_task.done():
                pipe_task.cancel()
            # ✅ 断开兜底：GeneratorExit 落在 yield 处不经过 except，
            # 幂等清理未终态的任务（finish_task 未执行过才会命中）。
            if has_active_task(task_id):
                await finish_task(task_id, "stopped", "客户端断开，任务已终止")

    return StreamingResponse(with_heartbeat(event_stream()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- 任务控制 ----------
@router.post("/task/{task_id}/control")
async def task_control(task_id: str, body: dict):
    action = body.get("action", "")
    if action in ("pause", "resume", "stop"):
        ok = request_control(task_id, action)
        if not ok:
            # ✅ 僵尸任务可停（2026-09-17）：request_control 只认**本进程内存态**，
            #    但任务栏数据源（task_registry 表）只要 DB 还是 running/paused 就一直
            #    显示为运行中。多 worker 部署 / 进程异常重启 / 生成流异常退出都可能留下
            #    「DB 仍在跑、内存已无此任务」的行 —— 旧实现此时直接返回"任务不存在"，
            #    用户点停止永远无效，任务栏留下清不掉的僵尸。
            #    这里退化为「DB 态直接落终态」，保证停止按钮对所有可见任务都有效。
            cur = await _stopped_orphan_rows([task_id])
            if cur:
                return {
                    "ok": True, "action": action, "orphaned": True,
                    "message": "任务已无运行实例（可能由外部进程残留），已标记为已停止",
                }
            return {"ok": False, "message": "任务不存在或已结束"}
        if action == "pause":
            from app.services.ai.task_registry import set_task_status
            await set_task_status(task_id, "paused")
        elif action == "resume":
            from app.services.ai.task_registry import set_task_status
            await set_task_status(task_id, "running")
        elif action == "stop":
            from app.services.ai.task_registry import set_task_status
            await set_task_status(task_id, "stopped")
        return {"ok": True, "action": action}
    return {"ok": False, "message": "未知操作"}


async def _stopped_orphan_rows(task_ids: list[str]) -> int:
    """把「DB 仍 running/paused 但本进程无内存态」的任务批量落终态 stopped。

    返回实际迁移的行数。仅改 DB（内存态本就不存在），并触发一次活动广播，
    使侧边栏任务窗口立刻反映结果。
    """
    if not task_ids:
        return 0
    conn = await get_conn()
    try:
        placeholders = ",".join("?" * len(task_ids))
        cur = await conn.execute(
            f"UPDATE task_registry SET status='stopped', message=?, updated_at=? "
            f"WHERE id IN ({placeholders}) AND status IN ('running','paused')",
            ["无运行实例，已终止", datetime.now().isoformat(), *task_ids])
        await conn.commit()
        changed = cur.rowcount or 0
    except Exception as e:
        logger.warning("_stopped_orphan_rows 失败（已忽略）: %s", e)
        return 0
    if changed:
        _ab.notify()
    return changed


# ---------- 任务状态查询（SSE 断线/页面刷新后重新挂接） ----------

_TERMINAL_STATUSES = {"completed", "failed", "stopped"}

#: task_type → checkpoint kind → 允许回传给前端的字段白名单
#  （白名单而非整体透传：checkpoint 里可能含内部字段，且避免未来新增字段泄漏）
_CHECKPOINT_KINDS = {
    "outline_generation": ("outline_result",
                           # ✅ event / partial 必须回传（2026-09-25 补齐）：
                           #    断线重挂接时前端要靠 event 区分「用户主动停止
                           #    （stopped，可继续补生成）」与「异常中断
                           #    （error，需整轮重来）」，靠 partial 区分
                           #    「完整目录」与「只生成了一部分的目录」。缺这两个
                           #    字段时，前端只能笼统提示"任务已结束"，用户不知道
                           #    该不该保存已生成的那部分 —— 而部分成果是有效的。
                           ("event", "partial", "outline", "review",
                            "failed_chapters", "failed_count")),
    "content_generation": ("content_result",
                           # ✅ run_words / over_count 必须入白名单（2026-09-23）：
                           #    断线重挂后前端要显示「本次生成 X 字 / 超字数 N 章」，
                           #    缺这两项时重挂页面上的统计全为 0，与在线看到的不一致。
                           ("event", "message", "done", "total", "failed_count",
                            "failed_sections", "words", "run_words",
                            "over_count", "word_count")),
}


def _attach_checkpoint_result(row, result: dict) -> None:
    """把 checkpoint_json 里的任务成果附到 task_status 响应（按 task_type 白名单）。

    ✅ 目录成果恢复：断线/刷新后 completed 的目录仅存在于 SSE 事件中，
    通过 checkpoint 回传 outline 载荷，前端可走与在线一致的保存确认流程。
    ✅ 正文成果恢复（2026-09-16 增强）：正文已逐章落库，但「哪些章失败、为什么」
    只在 SSE 里出现过；断线重挂接时由 content_result 回传失败明细与累计字数。
    """
    spec = _CHECKPOINT_KINDS.get(row["task_type"])
    if not spec or not row["checkpoint_json"]:
        return
    kind, fields = spec
    try:
        ckpt = json.loads(row["checkpoint_json"])
    except (TypeError, ValueError):
        return
    if not isinstance(ckpt, dict) or ckpt.get("kind") != kind:
        return
    result[kind] = {k: v for k, v in ckpt.items() if k in fields}



@router.get("/task/{task_id}")
async def task_status(task_id: str):
    """查询单个任务状态。

    内存优先（进行中任务含实时进度），回退 DB task_registry 表
    （历史任务 / 进程重启后的遗留任务）。
    """
    state = _tr._tasks.get(task_id)
    if state:
        # ✅ 修复：内存分支补齐 message/updated_at —— 旧实现缺这两项，而前端
        #    pollTaskUntilTerminal 读 data.message 展示终态原因；任务刚进内存终态、
        #    finish_task 尚未 pop 的窗口内，断线重挂会拿不到 message（与 DB 分支口径不一）。
        res = {
            "task_id": task_id,
            "task_type": state.get("type", ""),
            "status": state.get("status", "running"),
            "progress": state.get("progress", 0.0),
            "message": state.get("message", ""),
            "scheme_id": state.get("scheme_id", ""),
            "live": True,
        }
        # ✅ 成果补挂（2026-09-20 · 断线重挂空窗修复）：finish_task 已把状态
        #    置为终态、但尚未 pop 内存条目时，重挂接走的是**内存分支** —— 旧实现
        #    直接 return，读不到 DB checkpoint，于是前端拿到 status=stopped
        #    却没有 outline_result，误判"无成果"并整轮重来（跑了几分钟白跑）。
        #    仅终态才补挂：进行中任务读 DB 属无谓开销（且此时本就无成果）。
        if res["status"] in _TERMINAL_STATUSES:
            try:
                conn = await get_read_conn()
            except Exception:
                conn = None
            if conn is not None:
                try:
                    cur = await conn.execute(
                        "SELECT task_type, checkpoint_json FROM task_registry "
                        "WHERE id=?", (task_id,))
                    row = await cur.fetchone()
                    if row:
                        _attach_checkpoint_result(row, res)
                except Exception:
                    logger.debug("内存终态补挂 checkpoint 失败（忽略）", exc_info=True)
                finally:
                    try:
                        await release_read_conn(conn)
                    except Exception:
                        pass
        return res
    conn = await get_read_conn()
    try:
        cur = await conn.execute(
            "SELECT id, task_type, status, progress, message, scheme_id, updated_at, checkpoint_json"
            " FROM task_registry WHERE id=?",
            (task_id,))
        row = await cur.fetchone()
    finally:
        await release_read_conn(conn)
    if not row:
        raise HTTPException(404, "任务不存在")
    result = {
        "task_id": row["id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "progress": row["progress"] or 0.0,
        "message": row["message"] or "",
        "scheme_id": row["scheme_id"] or "",
        "updated_at": row["updated_at"],
        "live": False,
    }
    # ✅ 成果恢复：断线/刷新后终态任务的成果（目录树 / 正文失败明细）仅在 SSE 事件中，
    #    通过 checkpoint 回传，前端可走与在线一致的处理流程。
    _attach_checkpoint_result(row, result)
    return result


@router.get("/tasks")
async def list_tasks(scheme_id: str = "", limit: int = 20):
    """列出最近任务（可按方案过滤），前端刷新后据此重新挂接进行中任务。"""
    limit = max(1, min(limit, 100))
    conn = await get_read_conn()
    try:
        if scheme_id:
            cur = await conn.execute(
                "SELECT id, task_type, status, progress, message, updated_at"
                " FROM task_registry WHERE scheme_id=? ORDER BY created_at DESC LIMIT ?",
                (scheme_id, limit))
        else:
            cur = await conn.execute(
                "SELECT id, task_type, status, progress, message, scheme_id, updated_at"
                " FROM task_registry ORDER BY created_at DESC LIMIT ?",
                (limit,))
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await release_read_conn(conn)
    # 合并内存实时状态（DB 的 status/progress 落后于内存）
    for t in rows:
        st = _tr._tasks.get(t["id"])
        if st:
            t["status"] = st.get("status", t["status"])
            t["progress"] = st.get("progress", t["progress"])
            t["live"] = True
        else:
            t["live"] = False
    return {"tasks": rows}
