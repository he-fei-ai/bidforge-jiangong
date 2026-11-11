"""正文生成 SSE 终态载荷拼装（B1 拆分 · 自 sse_handlers 下沉）。

completed / stopped / error / 断线兜底 / 空章节 五条收尾路径共用这里的
**唯一拼装点**：字段口径只允许在一处定义，逐处手写必然漂移 —— 历史上就出现过
「completed 带 failed_count、error 不带」的口径矛盾，导致前端终态汇总与
stats 对不上。

与拆分前的闭包实现相比，唯一差异是「显式收参」：
  · 运行态（ckpt / 进度统计 / 失败清单 / 顺序留痕等）由调用方传入，不读闭包；
  · 进度折算 `_progress_now()` 由 sse_handlers 内的同名薄包装在调用前求值后传入，
    保留「省略 progress 时回落到加权进度」的原有语义。
字段集合、取值口径、告警文案与拆分前逐字一致（行为金样见
tests/test_content_sse_contract.py）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from app.services.content_utils import (
    summarize_fact_mapping,
    verify_generation_order,
    word_audit_summary,
)

logger = logging.getLogger(__name__)


def build_content_checkpoint(
    event: str,
    message: str = "",
    *,
    ckpt: dict,
    task_id: str | None,
    total: int,
    done_ids: set,
    failed_ids: set,
    failed_reasons: Mapping[str, Any],
    prog: Mapping[str, Any],
    run_stats: Mapping[str, Any],
    word_tol: float | None,
    fact_autofix_summary: dict,
    dispatched_order: Sequence[Any],
    expected_order: Sequence[Any],
    fact_audit_by_leaf: Mapping[str, Any],
    order_warned: dict,
) -> dict:
    """正文生成终态 checkpoint 的**唯一**拼装点。

    ✅ BUG 修复（2026-10-05 · 正文生成 R-1 · checkpoint 统计字段丢失）：
    本次运行的三个统计量（累计字数 / 本次生成字数 / 超字数章数）此前由
    **各收尾路径自己往 ckpt 里写**，而只有 completed 路径记得写全：
      · completed   → words + run_words + over_count ✅
      · 用户停止     → 仅 words，run_words/over_count 恒 0
      · 取消(断连等) → **一个都没写**（ckpt 保持初始值 0）
      · 整批异常     → **一个都没写**
      · 断线兜底     → **一个都没写**
    后果：用户点停止 / 刷新页面 / 弱网断连后，前端重挂接走
    `contentResultSummary(content_result)`（读 done/total/words/
    over_count）永远读到 0 —— 明明后台已经写了几万字，界面却显示
    「已完成 0/N 章，共 0 字」，与在线看到的横幅完全对不上。
    修法：把回填放进**唯一拼装点**（本函数），五条路径一次性全部正确，
    结构上不可能再漂移；调用点不再各自写 ckpt 统计量。
    """
    ckpt["words"] = int(prog.get("words", 0) or 0)
    ckpt["run_words"] = ckpt["words"]
    ckpt["over_count"] = int(run_stats.get("over", 0) or 0)
    # ✅ R66（2026-10-11）字数可追溯：汇总随所有终态载荷一并回填
    #    （与 words/over_count 同一"唯一拼装点"纪律，五条收尾路径全覆盖）。
    #    统计只服务于展示与审计，任何异常不得影响 checkpoint 落盘。
    #    word_tol 为 None 表示容差未建立（拆分前该名字未绑定的降级路径），
    #    与拆分前同口径：word_audit 记空 dict。
    if word_tol is None:
        ckpt["word_audit"] = {}
    else:
        try:
            ckpt["word_audit"] = word_audit_summary(run_stats.get("audit_rows") or [], tolerance=word_tol)
        except Exception:
            # ✅ R208：空 dict 与「本轮无数据」不可混淆 —— 统计计算失败必须留痕，
            #    否则断线重挂 / 审核预检把失败当「0 项」，运维也无从区分。
            ckpt["word_audit"] = {}
            ckpt["word_audit_error"] = True
            logger.warning("正文终态 word_audit 汇总计算失败（task=%s）", task_id, exc_info=True)
    # ✅ R77b（2026-10-15）：事实冲突定点修复汇总随所有终态载荷回填
    #    （与 word_audit 同一"唯一拼装点"纪律，五条收尾路径全覆盖；
    #    断线重挂/审核预检经白名单同读本键）。纯累加器、零异常面。
    ckpt["fact_autofix_summary"] = fact_autofix_summary
    # ✅ R105（G1 · 生成后顺序校验留痕）：派发序 vs 目录 DFS 期望序逐位
    #    比对（verify_generation_order 纯函数、不抛异常、不改输入）。
    #    期望序未建立（早退/早期异常，本次一章都没派发）时不产键，
    #    载荷与引入前同构；失守即 WARNING 点名首个错位与缺/多章节
    #    （order_warned 闸门防多条收尾路径重复刷屏）。
    if expected_order:
        try:
            _oc = verify_generation_order(dispatched_order, expected_order)
            ckpt["order_check"] = _oc
            if not _oc.get("ok") and not order_warned["flag"]:
                order_warned["flag"] = True
                logger.warning(
                    "正文生成顺序校验失守：派发序与目录 DFS 序不一致"
                    "（task=%s · 首个失配位=%s · 未派发=%s · 乱序多出=%s）",
                    task_id,
                    _oc.get("first_mismatch_index"),
                    (_oc.get("missing") or [])[:5],
                    (_oc.get("unexpected") or [])[:5],
                )
        except Exception:
            # ✅ R208：同 word_audit —— 顺序校验统计失败不得伪装成「无问题」。
            ckpt["order_check"] = {}
            ckpt["order_check_error"] = True
            logger.warning("正文终态 order_check 计算失败（task=%s）", task_id, exc_info=True)
    # ✅ R105（G3 · 逐章注入留痕汇总）：多少章有留痕、多少条事实
    #    因预算没进提示词、落在哪几章（纯读累加器，零异常面）。
    try:
        _fd_total = 0
        _fd_secs = 0
        _ft_total = 0
        _ft_secs = 0
        for _v in fact_audit_by_leaf.values():
            _n = int(_v.get("budget_dropped_count") or 0)
            if _n > 0:
                _fd_total += _n
                _fd_secs += 1
            # ✅ 账实相符（2026-11-04）：截断注入单列统计（≠ 丢弃）。
            _tn = int(_v.get("truncated_count") or 0)
            if _tn > 0:
                _ft_total += _tn
                _ft_secs += 1
        ckpt["fact_injection_summary"] = {
            "sections": len(fact_audit_by_leaf),
            "budget_dropped_total": _fd_total,
            "budget_dropped_sections": _fd_secs,
            "truncated_total": _ft_total,
            "truncated_sections": _ft_secs,
        }
    except Exception:
        # ✅ R208：同族第三处 —— 事实注入留痕统计失败不得伪装成「零丢弃」。
        ckpt["fact_injection_summary"] = {}
        ckpt["fact_injection_summary_error"] = True
        logger.warning("正文终态 fact_injection_summary 计算失败（task=%s）", task_id, exc_info=True)
    return {
        "event": event,
        "message": message,
        # R175 BUG-4：done 恒为**成功**章节数；processed = 已处理（成功 + 失败），
        # 与运行期 progress 事件同口径。processed 为加法字段，旧消费方读 done/total
        # 不受影响；ckpt 未写 processed 时按 done + failed 反推。
        "done": ckpt.get("done", len(done_ids)),
        "processed": int(ckpt.get("processed") or (ckpt.get("done", len(done_ids)) + len(failed_ids))),
        "total": ckpt.get("total", total),
        # ✅ R215（2026-11-11 · P3）：计数口径与在线终态（_stopped_payload /
        #    _error_payload 的 max(total - len(done_ids), 0)）同源 —— R194 已确立
        #    「done_ids 口径，_failed_reasons 只作明细」，而本通道（断线重挂读的
        #    checkpoint）此前仍用 len(failed_ids) ⇒ 用户停止/断线后重挂摘要的
        #    失败数与停止瞬间横幅突变（差额 = 未处理章节）。
        "failed_count": max(int(total) - len(done_ids), 0),
        "failed_sections": list(failed_reasons.values())[:50],
        "words": ckpt.get("words", 0),
        # ✅ 本次运行统计（前端终态横幅 / 重挂接页展示）：
        #    run_words = 本次实际生成字数；over_count = 超字数章节数。
        "run_words": ckpt.get("run_words", 0),
        "over_count": ckpt.get("over_count", 0),
        # ✅ R199（2026-11-09）：本次运行配图总数（章节成功落库时逐章累加），
        #    断线重挂摘要经 contentResultSummary 透出「配图 N 张」。缺键回退 0
        #    （加法式，旧 checkpoint 兼容）。
        "charts": int(ckpt.get("charts", 0) or 0),
        # ✅ R66（2026-10-11）：逐章目标/实际/偏差明细 + 达标统计
        #    （word_audit_summary 结构，断线重挂与审核预检同读本键）。
        "word_audit": ckpt.get("word_audit", {}),
        # ✅ R77b（2026-10-15）：事实冲突定点修复汇总（sections /
        #    fixed_sections / conflicts / applied / rejected）。
        "fact_autofix_summary": ckpt.get("fact_autofix_summary", {}),
        # ✅ R105（2026-10-18 · G1/G3/G4 断线收口）：三键与上方回填
        #    同源（唯一拼装点纪律，五条收尾路径全覆盖）。
        #    order_check = 派发序 vs 目录 DFS 序逐位比对结果
        #    （verify_generation_order 纯数据形态）；期望序未建立时为空 dict。
        #    fact_mapping = 显式「章节 → 所需事实」映射表全量
        #    （chapter_fact_mapping 产物，与逐章注入同一判据）。
        #    fact_injection_summary = 逐章注入留痕汇总。
        "order_check": ckpt.get("order_check", {}),
        "fact_mapping": ckpt.get("fact_mapping", []),
        "fact_injection_summary": ckpt.get("fact_injection_summary", {}),
    }


def build_stopped_payload(
    message: str,
    *,
    progress: float,
    task_id: str | None,
    total: int,
    done_ids: set,
    failed_reasons: Mapping[str, Any],
    ckpt: Mapping[str, Any],
    standard_summary: dict,
    fact_autofix_summary: dict,
    fact_audit_by_leaf: Mapping[str, Any],
) -> dict:
    """`stopped` 终态事件的**唯一**拼装点。

    ✅ BUG 修复（2026-10-05 · 正文生成 R-2 · 两条 stopped 路径字段漂移）：
    「用户主动停止」与「CancelledError（服务端超时 / 防僵尸替换 / 优雅关闭）」
    两条路径此前各自手写载荷，实测缺失如下：
      · 用户停止 → progress ✅ / failed_count ✅ / failed_sections ✅
                      / standard_summary ✅
      · 取消路径 → **progress 缺** / standard_summary 缺
    前端 `stopped` 分支读 evt.progress 决定进度条停在哪个值；缺失时进度条
    冻结在最后一次心跳的值（可能是 0.3），与 standard_summary 丢失一起让
    「服务端超时中断」这条路径最难排查。

    `progress` 由调用方（sse_handlers 的薄包装）求值后传入：省略时按当前
    加权进度取值并过不可回退护栏（`_progress_now`）。
    """
    return {
        "event": "stopped",
        "task_id": task_id,
        "progress": progress,
        "message": message,
        "failed_count": max(total - len(done_ids), 0),
        "failed_sections": list(failed_reasons.values())[:50],
        "standard_summary": standard_summary,
        # ✅ R77b：事实冲突定点修复汇总随 stopped 一并下发 —— 终态
        #    三处拼装点（stopped / error / completed）字段完全一致
        #    （R66/R74 同款纪律：用户中途停止也要看得到修复成效）。
        "fact_autofix_summary": fact_autofix_summary,
        # ✅ R105：顺序校验 + 事实映射汇总与 completed / error 同键同口径
        #    （三处拼装点字段一致纪律；全量映射在 checkpoint 面，
        #    SSE 事件只带 summarize_fact_mapping 汇总）。
        "order_check": ckpt.get("order_check", {}),
        "fact_mapping_stats": summarize_fact_mapping(ckpt.get("fact_mapping") or [], fact_audit_by_leaf),
    }


def build_error_payload(
    message: str,
    *,
    include_sections: bool = True,
    progress: float,
    task_id: str | None,
    total: int,
    done_ids: set,
    failed_reasons: Mapping[str, Any],
    ckpt: Mapping[str, Any],
    standard_summary: dict,
    fact_autofix_summary: dict,
    fact_audit_by_leaf: Mapping[str, Any],
) -> dict:
    """`error` 终态事件的**唯一**拼装点。

    ✅ BUG 修复（2026-10-05 · 正文生成 R-3 · 两条 error 路径字段漂移）：
    「全章失败」与「整批异常」（外层 except Exception）两条路径手写的 error
    载荷形状不同 —— 后者**完全没有** total / failed_count / failed_sections
    / progress / standard_summary。而整批异常恰恰是最需要明细的场景：异常
    可能发生在任何章节进入 gen_one **之前**（例如章节树查询失败），此时一条
    section_error 都没下发过，失败明细却随载荷一起被丢弃，用户只看到一句
    「正文生成失败：'NoneType' object has no attribute 'fetchall'」。
    """
    payload = {
        "event": "error",
        "task_id": task_id,
        "message": message,
        "progress": progress,
        "total": total,
        "failed_count": max(total - len(done_ids), 0),
        "standard_summary": standard_summary,
        "fact_autofix_summary": fact_autofix_summary,
        # ✅ R105：与 stopped / completed 两处拼装点同键同口径。
        "order_check": ckpt.get("order_check", {}),
        "fact_mapping_stats": summarize_fact_mapping(ckpt.get("fact_mapping") or [], fact_audit_by_leaf),
    }
    if include_sections and failed_reasons:
        payload["failed_sections"] = list(failed_reasons.values())[:50]
    return payload
