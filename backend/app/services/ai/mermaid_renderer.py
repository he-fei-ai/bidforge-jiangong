"""Mermaid 图表渲染引擎（facade）。

本文件是拆分后的统一入口：保留向后兼容的公共 API，
实际渲染逻辑已拆分到 7 个图表子文件：

  - mermaid_common.py         公共工具（字体、文本绘制、300 DPI 装饰器）
  - mermaid_flowchart.py      流程图
  - mermaid_gantt.py          甘特图
  - mermaid_architecture.py   组织架构图
  - mermaid_comparison.py     对比图 / 饼图 / 柱状图
  - mermaid_labor.py          劳动力配置图
  - mermaid_layout.py         总平面布置图
  - mermaid_timeline.py       关键里程碑时间轴

外部调用方继续使用：
    from .mermaid_renderer import render_mermaid_to_bytes, ChartCache

所有拆分前可用的内部函数（_render_xxx_image_v2、_parse_xxx、_repair_xxx_data 等）
均通过 re-export 暴露，import 路径不变。
"""
from __future__ import annotations

import asyncio
from io import BytesIO
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time

from .mermaid_service import MermaidRenderError, get_mermaid_service_client


logger = logging.getLogger(__name__)


# ============================================================
# P0-1 性能优化：常驻渲染事件循环
# 旧实现每次 HTTP 尝试都在 to_thread 线程内新建临时事件循环（asyncio.run），
# httpx AsyncClient（mermaid_service 按 loop 缓存）随之反复创建/销毁。
# 改为单例常驻 loop 线程 + run_coroutine_threadsafe 提交，连接池可复用。
# ============================================================
_RENDER_LOOP: asyncio.AbstractEventLoop | None = None
_RENDER_LOOP_LOCK = threading.Lock()


def _get_render_loop() -> asyncio.AbstractEventLoop:
    global _RENDER_LOOP
    if _RENDER_LOOP is None:
        with _RENDER_LOOP_LOCK:
            if _RENDER_LOOP is None:
                _loop = asyncio.new_event_loop()

                def _run_loop() -> None:
                    asyncio.set_event_loop(_loop)
                    _loop.run_forever()

                threading.Thread(
                    target=_run_loop, name="mermaid-render-loop", daemon=True).start()
                _RENDER_LOOP = _loop
    return _RENDER_LOOP


def _safe_asyncio_run(coro):
    """在常驻渲染事件循环中执行协程并等待结果"""
    loop = _get_render_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


# ============================================================
# ✅ R11 修复（2026-09-22）：渲染去重 + 空输入静默
#
# 背景：logs/backend.log 中 22 次连续同一签名 "[渲染] 空输入直接返回 None"，
# 说明同一份正文在生成链路里被反复送进渲染器，或空输入被重复调用。
# 空输入是**正常控制流**（无图可渲染），却打 WARNING，日志噪声大。
#
# 两处改动：
# 1. 空输入改为 DEBUG 级日志（不再刷屏 WARNING），业务语义不变（仍返回 None）；
# 2. 新增模块级 LRU 缓存（键=chart_type + code + duration/unit/tick/skip_http/allow_pil），
#    同一份代码在短时间窗内不会重复触发渲染（PIL v2 渲染 CPU 密集）。
#    缓存**只缓存非 None 结果**（避免把错误固化）；容量 64 项，超出时按 LRU 淘汰。
# ============================================================
from collections import OrderedDict

_RENDER_CACHE_MAX = 64
_render_cache: "OrderedDict[tuple, BytesIO | None]" = OrderedDict()
_render_cache_lock = threading.Lock()


def _code_cache_ident(mermaid_code) -> str:
    """把图表载荷转成缓存键里的**无歧义**标识。

    ✅ BUG 修复（2026-09-25 · 长载荷缓存串图）：
      旧实现 `str(mermaid_code)[:8192]` —— 超过 8192 字符的载荷被**截断**成前缀
      做键。登记的正文图表块上限是 `MAX_INLINE_CODE_BLOCK_LINES=500` 行，一份
      500 行的 gantt / 上百个 zone 的 layout / 大矩阵 labor 载荷轻易超过 8KB，
      于是"前 8192 字符相同、其后不同"的两张**不同**图表共用同一个缓存键：
      后一张直接命中前一张的 PNG，成稿里出现张冠李戴的图（且完全静默）。
      现改为：短载荷原样入键（保持既有键值、测试与磁盘缓存兼容），超长载荷
      用 sha1 摘要代替 —— 键空间无歧义，且长度可控。
    """
    if mermaid_code is None:
        return ""
    s = str(mermaid_code)
    if len(s) <= 8192:
        return s
    return "sha1:" + hashlib.sha1(
        s.encode("utf-8", "surrogatepass")).hexdigest()


def _render_cache_key(mermaid_code: str, chart_type: str,
                     duration: int, unit: str, tick: int,
                     skip_http: bool, allow_pil: bool) -> tuple:
    """构造缓存键。仅用可 hash 的元组，避免对 BytesIO 做 hash。"""
    return (
        chart_type,
        _code_cache_ident(mermaid_code),
        duration, unit, tick, skip_http, allow_pil,
    )


def clear_render_cache() -> None:
    """清空渲染缓存（供测试 / 部署后手动调用）。"""
    with _render_cache_lock:
        _render_cache.clear()


def _render_cache_get(key: tuple) -> BytesIO | None:
    with _render_cache_lock:
        if key in _render_cache:
            _render_cache.move_to_end(key)
            cached = _render_cache[key]
            if cached is None:
                return None
            # ✅ 返回副本：BytesIO 有读取位置（seek/read 状态），把缓存中的同一
            #    对象直接交给多个调用方时，任一方 read()/seek() 都会污染其他方
            #    与后续命中（导出线程与 /charts/render 并发命中同一键时可复现）。
            try:
                return BytesIO(cached.getvalue())
            except Exception:
                return None
    return None


def _render_cache_put(key: tuple, value: BytesIO | None) -> None:
    """仅缓存非 None 结果，错误结果不入缓存。"""
    if value is None:
        return
    with _render_cache_lock:
        _render_cache[key] = value
        _render_cache.move_to_end(key)
        # LRU 淘汰
        while len(_render_cache) > _RENDER_CACHE_MAX:
            _render_cache.popitem(last=False)


def render_mermaid_to_bytes(
    mermaid_code: str,
    chart_type: str = "flowchart",
    *,
    duration: int = 90,
    unit: str = "天",
    tick: int = 5,
    skip_http: bool = False,
    allow_pil: bool = True,
) -> BytesIO | None:
    """
    将 Mermaid 代码/JSON 数据渲染为 PNG 图片字节流（商业级渲染管道 — 无降级 V5.0）

    分层策略（严格执行"不做降级、按最优标准渲染"）：
      1. Level 1.5: Mermaid HTTP Service (Docker/Railway) → 2x 高清 PNG（所有图表类型）
      2. Level 2: PIL/Pillow 自定义 v2 商业级渲染器（流程图/甘特图/架构图/对比图/劳动力图/布局图/时间轴）
         • 2.5x 超采样抗锯齿 + 300 DPI 元数据（@_render_hq 装饰器）
      3. 返回 None（调用方自行处理，不做降级）

    V7.0 不降级开关：
      - allow_pil=False 时，Mermaid 语法载荷（flowchart/gantt/pie 等）在 HTTP Service 失败后
        **不再回退 PIL v2 自绘**（版式与 mermaid 原生不一致，属于降级），直接返回 None 由调用方占位
      - JSON 数据载荷（layout/timeline 的 zones/milestones）不受此开关限制——
        PIL v2 是其唯一渲染路径（mermaid 引擎无法消费 JSON 数据），不存在"降级"语义

    Args:
        mermaid_code: Mermaid 代码字符串 或 结构化 JSON 字符串
        chart_type: 图表类型 (flowchart|gantt|architecture|labor|comparison|layout|timeline|chart)
        duration: 甘特图总工期
        unit: 甘特图时间单位
        tick: 甘特图刻度间隔
        skip_http: BUG-E-02 修复：跳过 HTTP Service 渲染路径（当 HTTP 服务不可用时）
                   直接走 PIL v2 渲染路径，节省重试超时等待
        allow_pil: V7.0 不降级开关——False 时 Mermaid 语法载荷禁用 PIL 兜底（默认 True 保持兼容）

    Returns:
        BytesIO of PNG image, or None if all levels fail
    """

    import time as _time

    _t0 = _time.perf_counter()
    logger.info("[渲染] 开始 chart_type=%s code长度=%d", chart_type, len(mermaid_code or ""))

    # ✅ R11 修复：空输入改为 DEBUG 级日志。空输入是**正常控制流**（无图可渲染），
    # 不应作为 WARNING 刷屏（此前 22 次连续同一签名淹没真实错误信号）。
    if not mermaid_code or not str(mermaid_code).strip():
        logger.debug("[渲染] 空输入直接返回 None（chart_type=%s）", chart_type)
        return None

    chart_type = (chart_type or "flowchart").lower().strip()
    code = str(mermaid_code).strip()

    # ✅ R11 修复：命中缓存直接返回，跳过 PIL v2 / HTTP 渲染路径（CPU 密集）
    _cache_key = _render_cache_key(code, chart_type, duration, unit, tick, skip_http, allow_pil)
    _cached = _render_cache_get(_cache_key)
    if _cached is not None:
        logger.debug("[渲染] 缓存命中 chart_type=%s code长度=%d 耗时=%.3fs",
                     chart_type, len(code), _time.perf_counter() - _t0)
        return _cached

    # ⚠️ 语句分隔符归一已**下移**到「载荷归一化（包装器解包）」之后：
    #    旧实现把它放在解包之前，内层 `{"code": "graph TD; A-->B"}` 的分号从未被
    #    归一 —— 解包出的单行分号 Mermaid 会让所有按行解析的 PIL 渲染器吞掉连线。

    logger.info("[渲染] chart_type=%s code前80字=%r", chart_type, code[:80])

    # ---------- 解析 chart 通配符（自动从内部 type 字段识别） ----------
    if chart_type == "chart":
        try:
            parsed = json.loads(code)
            if isinstance(parsed, dict):
                inner_type = (parsed.get("type") or "").lower()
                if inner_type in {
                    "flowchart",
                    "gantt",
                    "architecture",
                    "labor",
                    "comparison",
                    "layout",
                    "timeline",
                }:
                    chart_type = inner_type
                    logger.info("[渲染] 通配符解析: chart→%s", chart_type)
        except (json.JSONDecodeError, TypeError):
            pass

    # ---------- V6.1 载荷归一化：解包数据库存储格式 ----------
    # 数据库/编排链路存储格式为 JSON 包装器：{"type": ..., "code": "<载荷>", "title": ...}
    # 不解包会导致：
    #   1. Mermaid 系类型（flowchart/gantt/comparison）：HTTP Service 收到 JSON 而非
    #      Mermaid 语法 → 第一优先方案从未真正执行成功，白白消耗重试等待
    #   2. layout/timeline：提示词 v2.1 实际产出 JSON 数据（zones/milestones），
    #      经 "code" 字段二次包装成双层 JSON → PIL v2 直接解析必失败
    # 归一化规则：
    #   - 包装器含 "code" 字符串字段 → 提取内层载荷
    #     · 内层为 Mermaid 语法（非 { 开头）→ 作为纯 Mermaid 代码（HTTP 可渲染）
    #     · 内层为 JSON → 解包为结构化数据（PIL v2 渲染），继承外层 title
    #   - 包装器无 "code" 字段（architecture/labor 等原生数据）→ 保持原样，PIL v2 直接处理
    try:
        _wrapper = json.loads(code)
        if isinstance(_wrapper, dict):
            _inner = _wrapper.get("code")
            if isinstance(_inner, str) and _inner.strip():
                _inner = _inner.strip()
                if not _inner.startswith("{"):
                    # 内层是纯 Mermaid 代码 → 第一优先方案（HTTP Service）可执行
                    code = _inner
                    logger.info("[渲染] 载荷归一化: 提取内层 Mermaid 代码 (%d 字符)", len(code))
                else:
                    # 内层仍是 JSON（layout/timeline 双层包装）→ 解包为数据载荷
                    try:
                        _inner_data = json.loads(_inner)
                        if isinstance(_inner_data, dict):
                            if _wrapper.get("title") and not _inner_data.get("title"):
                                _inner_data["title"] = _wrapper.get("title")
                            code = json.dumps(_inner_data, ensure_ascii=False)
                            logger.info(
                                "[渲染] 载荷归一化: 解包内层 JSON 数据 (keys=%s)",
                                list(_inner_data.keys())[:6],
                            )
                    except (json.JSONDecodeError, TypeError):
                        pass
    except (json.JSONDecodeError, TypeError):
        # 非 JSON → 纯 Mermaid 代码，无需归一化
        pass

    # ✅ Mermaid 语法载荷归一（校验侧与渲染侧**共用同一实现**，见
    #    chart_validators 的 strip_leading_mermaid_comments /
    #    normalize_mermaid_statements）：
    #    ① 语句分隔符：`;` 与换行在 Mermaid 中等价，但所有 PIL v2 渲染器都用
    #       split("\n") 逐行解析 —— 单行分号格式（`graph TD; A-->B`）会让整行被
    #       当作方向行、连线被吞；链式 `A --> B; B --> C` 则整块被跳过。
    #    ② 首部注释：AI 常在首行输出 `%% 标题` / `%%{init: ...}%%`，会让下游
    #       `code.startswith("gantt"/"graph")` 等判定落空（实测 gantt 会因此多画出
    #       一行垃圾任务，产物与无注释版不一致）。
    #    两侧同一实现可避免"校验通过、渲染出的却是另一张图"的口径分叉。
    #    ⚠️ JSON 载荷（`{`/`[` 开头）不做归一：字符串内的分号会被插入换行破坏 JSON。
    if code and not code.lstrip().startswith(("{", "[")):
        from app.services.chart_validators import (
            normalize_mermaid_statements,
            strip_leading_mermaid_comments,
        )

        code = normalize_mermaid_statements(strip_leading_mermaid_comments(code))
        if not code:
            logger.warning("[渲染] 归一化后为空载荷（chart_type=%s），返回 None", chart_type)
            return None

    # =========================================================
    # 渲染管道优先级（商业级最优策略 — 无降级 V5.0）
    # =========================================================
    #
    # 优先级策略（2026-08 增强版 — 严格执行"不做降级、按最优标准渲染"）：
    #   1. Level 1.5: Mermaid HTTP Service (Docker/Railway) — 主路径
    #      • 支持全部 7 种图表类型
    #      • 2.5x 高清 PNG，Mermaid 原生渲染引擎，质量最高
    #      • 本地 Docker / Railway Cloud / mmdc 自动检测
    #      • 超时 30s，最多重试 2 次
    #   2. Level 2: PIL/Pillow v2 商业级渲染器 — 零外部依赖，始终可用
    #      • 2.5x 超采样抗锯齿 + 300 DPI 元数据（@_render_hq 装饰器）
    #      • 支持全部 7 种图表类型（流程图/甘特图/架构图/对比图/劳动力图/布局图/时间轴）
    #      • 质量不低于 HTTP Service（超采样补偿）
    #   3. 不再使用 Level 1 (mmdc CLI) 降级路径：
    #      • mmdc 屏幕分辨率渲染（~96 DPI）低于 PIL v2（2.5x 超采样 + 300 DPI）
    #      • 移除 mmdc 可避免 60 秒超时等待、降低系统依赖
    #      • 符合"不做降级、按最优标准渲染"原则
    #   4. 不再使用 Level 3 (v1 PIL 渲染器) 降级路径：
    #      • v1 渲染器无抗锯齿、无 DPI 元数据，质量无法满足商业级要求
    #      • 所有 v1 渲染器已被功能更全的 v2 版本替代
    #
    # 核心变更（V5.0）：
    #   - 移除 Level 1 (mmdc) 降级回退
    #   - 确保所有图表类型仅走 Level 1.5 → Level 2 双路径
    #   - 当两者均失败时返回 None（调用方自行处理，不做降级）
    # =========================================================

    # ---------- Level 1.5 (主路径): Mermaid HTTP Service 渲染（2x 高清 PNG，Mermaid 语法载荷）----------
    # BUG-F-01 修复：_http_renderable 必须先初始化。
    # 原代码只在 skip_http=True 分支赋值，skip_http=False（HTTP 服务可用/检测结果未知）时
    # 直接引用未定义局部变量，抛 UnboundLocalError，导致所有图表渲染失败（图表全部丢失）。
    _http_renderable = not skip_http
    # BUG-E-02 修复：skip_http=True 时完全跳过 HTTP Service 路径
    # 在本地开发环境中 HTTP Service 通常不可用，每图尝试 30s 超时严重拖慢导出
    if skip_http:
        logger.info(
            "[渲染] Level 1.5 跳过: skip_http=True, 直接走 PIL v2 (chart_type=%s)", chart_type
        )
    # V6.1: HTTP Service 仅接受 Mermaid 语法。JSON 数据载荷（architecture/labor 的
    # 原生数据、layout/timeline 解包后的 zones/milestones）对 HTTP 服务必然失败，
    # 此类类型的第一优先方案即 PIL v2 商业级渲染器（唯一的原生渲染路径）。
    # 跳过无效 HTTP 尝试可省去 2 次失败重试（每次数秒），显著提升导出速度。
    _http_renderable = _http_renderable and not code.lstrip().startswith("{")
    if not _http_renderable:
        logger.info("[渲染] Level 1.5 跳过: JSON 数据载荷直接走 PIL v2 (chart_type=%s)", chart_type)
    _mermaid_client = get_mermaid_service_client(timeout=30)
    _http_bytes: bytes | None = None
    _max_retries = 2
    # BUG-F-02 修复：为整段 HTTP 尝试设置总预算（20s）。
    # 原代码每次尝试 30s、最多 2 次（+1s sleep），最坏可叠加到 62s，超过 _add_inline_chart
    # 外层超时保护后，渲染线程被 cancel 但无法回收（shutdown(wait=False)），
    # 滞留线程持续占用 httpx 连接与 CPU，多次导出后线程堆积导致导出卡顿/无响应。
    _http_total_budget = 20.0
    _http_attempt_start = _time.perf_counter()
    for _retry in range(_max_retries if _http_renderable else 0):
        if _retry > 0:
            logger.info("[渲染] Level 1.5 重试第 %d 次 (chart_type=%s)", _retry + 1, chart_type)
        _remaining = _http_total_budget - (_time.perf_counter() - _http_attempt_start)
        if _remaining <= 0:
            logger.warning(
                "[渲染] Level 1.5 总预算耗尽（%.1fs），放弃后续 HTTP 尝试", _http_total_budget
            )
            break
        try:
            _render_coro = asyncio.wait_for(
                _mermaid_client.render_mermaid_via_http(
                    code=code,
                    theme="neutral",
                    scale=2.5,
                ),
                timeout=_remaining,
            )
            # BUG 修复（P0，阻塞性）：原实现先 `_safe_asyncio_run(_render_coro)`（丢弃结果），
            # 紧接着对**同一个协程对象**二次 `_safe_asyncio_run(_render_coro)`。
            # 协程只能被 await 一次，第二次必然抛
            # RuntimeError("cannot reuse already awaited coroutine")，被下面的
            # `except Exception` 与更外层 `except Exception` 连续吞掉。
            # 后果：Level 1.5（Mermaid 原生引擎，主路径）**每一次尝试都必然失败**——
            #   · allow_pil=True  → 静默降级为 PIL v2 自绘（版式与 Mermaid 原生不一致）；
            #   · allow_pil=False（导出默认「不降级」）→ 所有 Mermaid 图直接渲染失败、文档出现红字占位。
            # 这里改为只 await 一次并取回结果。
            try:
                _http_bytes = _safe_asyncio_run(_render_coro)
            except Exception:
                # 协程泄漏防护：_safe_asyncio_run 异常时协程可能未被消费，
                # 显式关闭避免 "coroutine was never awaited"（对已完成协程是 no-op）
                if asyncio.iscoroutine(_render_coro):
                    try:
                        _render_coro.close()
                    except Exception as _e:
                        logger.debug("[silent-except] mermaid_renderer.py: line 232 - %s", _e)
                raise

            if _http_bytes and len(_http_bytes) > 100:
                logger.info(
                    "MermaidRenderer: 使用 HTTP Service 渲染成功 (%s, 2.5x, 重试=%d/2)",
                    chart_type,
                    _retry + 1,
                )
                return BytesIO(_http_bytes)
            else:
                logger.warning(
                    "[MermaidRenderer] HTTP 渲染返回空/过小数据 (%d bytes)，重试...",
                    len(_http_bytes or b""),
                )
                _http_bytes = None
        except MermaidRenderError as e:
            logger.warning("Mermaid HTTP Service 渲染失败（第%d次）: %s", _retry + 1, e)
            # ✅ BUG 修复：4xx（代码语法错/载荷非法）重试无意义，立即放弃 HTTP 路径
            if 400 <= getattr(e, "status_code", 0) < 500:
                break
        except Exception as e:
            logger.warning("Mermaid HTTP Service 异常（第%d次）: %s", _retry + 1, e)
        if _http_bytes is None and _retry < _max_retries - 1:
            __import__("time").sleep(1.0)

    # ---------- Level 1 (mmdc) 已移除 ----------
    # 原因：mmdc 渲染质量（~96 DPI 屏幕分辨率）低于 PIL v2 商业级渲染器（2.5x 超采样 + 300 DPI）
    # 且 mmdc 需要额外安装 Node.js 依赖，增加系统复杂度。
    # 符合"不做降级、按最优标准渲染"原则。

    # ---------- V7.0 不降级开关：Mermaid 语法载荷禁用 PIL 兜底 ----------
    # Mermaid 类图表的第一优先方案是 mermaid 原生引擎（前端 mermaid.js / HTTP Service），
    # PIL v2 自绘版式与原生不一致，属于降级输出。allow_pil=False 时直接返回 None，
    # 由调用方在文档中写入占位提示（用户可修复代码后重新导出）。
    # JSON 数据载荷（layout/timeline）不在此列——PIL v2 是其唯一渲染路径。
    _is_json_payload = code.lstrip().startswith("{")
    if not allow_pil and not _is_json_payload:
        logger.warning(
            "[渲染] 不降级模式: HTTP Service 失败且 allow_pil=False, "
            "Mermaid 载荷不回退 PIL (chart_type=%s)", chart_type)
        return None

    # ---------- Level 2 (PIL v2): 商业级渲染器（零外部依赖，始终可用）----------
    logger.info("[渲染] Level 2 (主路径): 尝试 PIL v2 商业级渲染器 chart_type=%s", chart_type)
    _level2_result: BytesIO | None = None
    try:
        # === 流程图（支持 mermaid 语法 + JSON 节点列表 + steps/edges 变体载荷）===
        if chart_type == "flowchart":
            if (
                code.startswith("flowchart")
                or code.startswith("graph")
                or code.startswith("%%{")
                or "-->" in code
            ):
                _level2_result = _render_flowchart_image_v2(code)
            if _level2_result is None:
                _plain_nodes: list[str] | None = None
                try:
                    parsed = json.loads(code)
                    if isinstance(parsed, dict):
                        # ✅ 2026-09-18：施工/工艺横向流程图 chart-json 载荷
                        #    （{"type":"flowchart","steps":[...],"edges":[...]}）——
                        #    归一（steps→nodes、variant→type、下标端点解析）后再转换，
                        #    显式 variant 经 variant_overrides 传给渲染器（优先于自动推断）。
                        from app.services.chart_validators import normalize_flowchart_data
                        parsed = normalize_flowchart_data(parsed) or parsed
                        nodes = parsed.get("nodes", [])
                        # 条件放宽：仅有 nodes 也能走完整渲染链路（函数内部自动补 edges）
                        if isinstance(nodes, list) and len(nodes) >= 2:
                            # 第一优先：JSON → 标准 Mermaid → v2 超采样渲染器（决策菱形 + 专业配色）
                            _edges_mermaid = flowchart_json_to_mermaid(parsed)
                            if _edges_mermaid:
                                _overrides = {
                                    n["id"]: n["type"]
                                    for n in nodes
                                    if isinstance(n, dict)
                                    and n.get("type") not in (None, "", "process")
                                }
                                _level2_result = _render_flowchart_image_v2(
                                    _edges_mermaid, variant_overrides=_overrides or None
                                )
                            # 次优先：完整流程图渲染器（带箭头标签，300 DPI）
                            if _level2_result is None:
                                _level2_result = _render_flowchart_with_edges(parsed)
                        if _level2_result is None and isinstance(nodes, list) and len(nodes) >= 2:
                            _plain_nodes = [str(n) for n in nodes if str(n).strip()]
                except (json.JSONDecodeError, TypeError):
                    pass
                if _level2_result is None and _plain_nodes is None:
                    _plain_nodes = _parse_mermaid_flowchart(code)
                # 最终防线：纯节点列表 → 构造顺序连线 Mermaid → v2 商业级渲染
                if _level2_result is None and _plain_nodes and len(_plain_nodes) >= 2:
                    _seq_code = "flowchart TD\n    " + " --> ".join(
                        f'N{i}["{str(n).replace(chr(34), chr(39))}"]'
                        for i, n in enumerate(_plain_nodes, 1)
                    )
                    _level2_result = _render_flowchart_image_v2(_seq_code)

        # === 甘特图（优先 v2 商业级：自动开始日期 + 真实进度）===
        if _level2_result is None and chart_type == "gantt":
            # ✅ BUG 修复（2026-09-13）：`"gantt" in code.lower()` 会命中 JSON 载荷
            #    （如 {"type":"gantt","tasks":[...]} 里含 "gantt" 字样），随后
            #    _parse_mermaid_gantt 把 JSON 当 Mermaid 语法逐行误解析，
            #    产生 "任务名 = {\"type"、总工期=90" 的错图，且正确的 JSON 分支被跳过。
            #    仅在载荷确为 Mermaid 语法（非 JSON）时才走语法解析。
            if "gantt" in code.lower() and not code.lstrip().startswith(("{", "[")):
                tasks = _parse_mermaid_gantt(code)
                if tasks:
                    max_end = max((t["end"] for t in tasks), default=duration)
                    plan = {
                        "type": "gantt",
                        "title": "施工进度计划",
                        "totalDays": max(max_end, duration),
                        "tasks": [
                            {
                                "id": idx + 1,
                                "name": t["task"],
                                "start": t["start"],
                                "end": t["end"],
                                "dependencies": [],
                                "isMilestone": t["start"] == t["end"],
                                # ✅ 透传阶段划分（`section xxx`），供渲染器绘制阶段带
                                "section": t.get("section", ""),
                            }
                            for idx, t in enumerate(tasks[:30])
                        ],
                    }
                    _level2_result = _render_gantt_image_v2(plan)
            if _level2_result is None:
                try:
                    parsed = json.loads(code)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                # ✅ BUG 修复（2026-09-17 · 占位型错配）：容器别名必须与**校验侧**
                #    `chart_validators.normalize_gantt_plan`（支持 tasks / items / rows）
                #    同口径。旧实现只认字面 "tasks" 键：AI 写
                #    `{"type":"gantt","items":[...]}` 或 `"rows":[...]` 时，
                #    管线校验**通过**（块保留在正文、登记 status=done），而渲染侧取不到
                #    tasks 直接返回 None → 导出成红字占位「[图 X-Y … — 渲染失败]」。
                #    证据：`_diagnostics/_chart_shape_audit.py` 的「items 别名+日期串」
                #    与「rows 别名」两例（合计 22 形态中的 2 例真实错配）。
                #    渲染器本身（_render_gantt_image_v2）内部已归一别名，缺的只是这里的分流判定。
                if isinstance(parsed, dict):
                    raw_tasks = parsed.get("tasks")
                    if not (isinstance(raw_tasks, list) and raw_tasks):
                        raw_tasks = None
                        for _alias in ("items", "rows"):
                            _cand = parsed.get(_alias)
                            if isinstance(_cand, list) and _cand:
                                raw_tasks = _cand
                                break
                    if raw_tasks:
                        has_enhanced = any(
                            isinstance(t, dict)
                            and (t.get("id") or t.get("dependencies")
                                 or t.get("critical"))
                            for t in raw_tasks
                        )
                        if has_enhanced:
                            _level2_result = _render_gantt_with_dependencies(parsed)
                        if _level2_result is None:
                            _level2_result = _render_gantt_image_v2(parsed)
                # ✅ BUG 修复（2026-09-16）：兜底分支必须与上面主分支**同一守卫口径**。
                #    历史缺陷：JSON 载荷（如 {"type":"gantt","tasks":[]} 或只有
                #    {"type":"gantt","note":"..."}）已被上面的 JSON 分支判为不可渲染，
                #    却又落到这里被 _parse_mermaid_gantt 逐行误解析，产出
                #    「任务名 = {"type"」的垃圾图并成功返回 —— 校验侧判非法、渲染侧却出图，
                #    口径完全相反，AI 修复/删除链路因此永远失效（正文留下垃圾图）。
                if _level2_result is None and not code.lstrip().startswith(("{", "[")):
                    # V6.1 无降级：宽松解析结果构造 plan dict，仍走 v2 商业级渲染器
                    tasks = _parse_mermaid_gantt(code)
                    if tasks:
                        max_end = max((t["end"] for t in tasks), default=duration)
                        plan = {
                            "type": "gantt",
                            "title": "施工进度计划",
                            "totalDays": max(max_end, duration),
                            "tasks": [
                                {
                                    "id": idx + 1,
                                    "name": t["task"],
                                    "start": t["start"],
                                    "end": t["end"],
                                    "dependencies": [],
                                    "isMilestone": t["start"] == t["end"],
                                }
                                for idx, t in enumerate(tasks[:30])
                            ],
                        }
                        _level2_result = _render_gantt_image_v2(plan)

        # === 架构图（商业级 v2 树形可视化）===
        if _level2_result is None and chart_type == "architecture":
            try:
                parsed = json.loads(code) if code.startswith(("{", "[")) else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                root_node = parsed
                if not (parsed.get("label") or parsed.get("name") or parsed.get("children")):
                    for key in ("root", "tree", "data", "node"):
                        nested = parsed.get(key)
                        if isinstance(nested, dict) and (
                            nested.get("label") or nested.get("name") or nested.get("children")
                        ):
                            root_node = nested
                            break
                    # 兼容 {type: "architecture", root: "xxx", nodes: [...]} 格式
                    if not (
                        root_node.get("label") or root_node.get("name") or root_node.get("children")
                    ):
                        nodes_list = parsed.get("nodes")
                        if isinstance(nodes_list, list) and nodes_list:
                            root_node = {
                                "label": str(parsed.get("root") or "组织架构图"),
                                "children": nodes_list,
                            }
                if root_node.get("label") or root_node.get("name") or root_node.get("children"):
                    _level2_result = _render_architecture_image_v2(root_node)
            # V6.1 无降级：v2 树形渲染失败即返回 None，不再回退 v1 表格渲染器

        # === 劳动力图（商业级 v2 四合一视图）===
        if _level2_result is None and chart_type == "labor":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                _level2_result = _render_labor_image_v2(parsed)
            # V6.1 无降级：v2 四合一视图渲染失败即返回 None，不再回退 v1 渲染器

        # === 对比图（商业级 v2 饼图 + 柱状图组合）===
        if _level2_result is None and chart_type == "comparison":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                _level2_result = _render_comparison_image_v2(parsed)
            if _level2_result is None:
                # V6.1: Mermaid pie / xychart-beta 代码 → 解析为数据后走 v2 渲染器
                # （HTTP Service 不可用时的高质量备份路径，渲染质量与主路径一致）
                _mermaid_cmp_data = _parse_mermaid_pie(code) or _parse_mermaid_xychart(code)
                if _mermaid_cmp_data:
                    logger.info(
                        "[渲染] 对比图: 解析 Mermaid 代码为 v2 数据 (items=%d)",
                        len(_mermaid_cmp_data.get("items", [])),
                    )
                    _level2_result = _render_comparison_image_v2(_mermaid_cmp_data)
            # V6.1 无降级：v2 饼图+柱状图渲染失败即返回 None，不再回退 v1 表格渲染器

        # === 布局图（商业级 v2 空间布局）===
        if _level2_result is None and chart_type == "layout":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                if not parsed.get("zones") and parsed.get("areas"):
                    parsed["zones"] = parsed["areas"]
                _level2_result = _render_layout_image_v2(parsed)
                if _level2_result is None:
                    repaired = _repair_layout_data(parsed)
                    if repaired:
                        _level2_result = _render_layout_image_v2(repaired)

        # === 时间轴/里程碑图（商业级 v2）===
        if _level2_result is None and chart_type == "timeline":
            try:
                parsed = json.loads(code) if code.startswith("{") else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                for m in parsed.get("milestones", []):
                    if isinstance(m, dict) and not m.get("name") and m.get("title"):
                        m["name"] = m["title"]
                _level2_result = _render_timeline_image_v2(parsed)
                if _level2_result is None:
                    repaired = _repair_timeline_data(parsed)
                    if repaired:
                        _level2_result = _render_timeline_image_v2(repaired)
            else:
                # ✅ Mermaid 原生 timeline 语法（HTTP Service 失败时的 PIL 兜底）
                _tl = _parse_mermaid_timeline(code)
                if _tl:
                    _level2_result = _render_timeline_image_v2(_tl)

    except Exception as e:
        logger.debug("PIL v2 渲染失败 (%s): %s", chart_type, e)
        logger.exception("详细错误：")

    if _level2_result is not None:
        elapsed = _time.perf_counter() - _t0
        logger.info(
            "[渲染] 成功 chart_type=%s 策略=PIL_v2 耗时=%.2fs 大小=%dKB",
            chart_type,
            elapsed,
            len(_level2_result.getvalue()) // 1024,
        )
        # ✅ R11 修复：把成功结果写入模块级 LRU 缓存（BytesIO 有状态，需 seek(0) 归位）
        try:
            _level2_result.seek(0)
        except Exception:
            pass
        _render_cache_put(_cache_key, _level2_result)
        return _level2_result

    elapsed = _time.perf_counter() - _t0
    logger.warning("[渲染] 全部渲染策略失败 chart_type=%s 耗时=%.2fs", chart_type, elapsed)
    logger.warning("[渲染] 建议检查: 1)HTTP服务是否可用 2)PIL v2渲染器是否正常")
    return None

# ✅ 深度审查修复（2026-09-20）：渲染器缓存版本号。
#    修改渲染逻辑 / 升级 mermaid 引擎后必须加 1，使旧缓存图片自动失效。
_RENDERER_VERSION = 1


class ChartCache:
    """图表渲染缓存 - 基于 Mermaid 代码/JSON 数据的哈希值

    避免重复渲染相同图表，提升批量导出性能。

    特性：
    - 自动缓存目录管理
    - 缓存统计信息（命中率、大小等）
    - LRU 淘汰 + 容量上限（V2.0 新增，修复 BUG-H-02）
    - 智能清理（按时间或大小）
    - 线程安全的文件操作（V1.1 新增锁保护）

    使用示例：
        cache = ChartCache("./cache/charts")
        # 尝试获取缓存
        cached = cache.get(mermaid_code)
        if cached:
            return cached
        # 渲染并缓存
        result = render_mermaid_to_bytes(mermaid_code, "flowchart")
        if result:
            cache.set(mermaid_code, result.getvalue())
    """

    def __init__(self, cache_dir: str = "", max_size_mb: float = 500.0):
        # ✅ 修复：默认用绝对路径（基于 app.config），避免依赖启动 CWD 导致缓存失效
        if not cache_dir:
            from app.config import CHARTS_DIR
            cache_dir = str(CHARTS_DIR)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_size_bytes = int(max_size_mb * 1024 * 1024)  # 默认 500MB
        self._stats = {"hits": 0, "misses": 0, "total_size": 0}
        # 使用可重入锁：set() 持锁期间会调用 _evict_if_needed() 再次加锁，
        # 非重入的 threading.Lock 会自死锁（V2.0 LRU 淘汰引入的回归缺陷）
        self._lock = threading.RLock()  # V1.1 新增：线程安全锁
        # V2.0 新增：LRU 访问顺序追踪 {cache_key: mtime}；`{key}.fmt` 为格式元数据键
        self._access_order: dict[str, float | str] = {}
        # P0-2 单飞去重：缓存 miss 后相同 key 的并发渲染共享一次（{key: threading.Event}）
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()
        # ✅ BUG 修复：从磁盘重建内存状态（LRU 顺序 + 总大小）
        self._sync_state_from_disk()

    def _sync_state_from_disk(self) -> None:
        """从磁盘重建内存状态（LRU 顺序 / 总占用）。

        ✅ BUG 修复：旧实现只在 __init__ 中把 total_size 置 0、_access_order 置空，
        从不扫描已有的缓存目录。后果：
          · 进程重启后 total_size 从 0 重新累计，**500MB 容量上限形同虚设** ——
            每重启一次就又能往磁盘写满 500MB，缓存目录可持续膨胀（图表缓存是
            2.5x 超采样 PNG，单张数百 KB，批量生成后很容易达到 GB 级）；
          · get_stats() 的 total_size 由磁盘现算，与 _stats["total_size"] 长期不一致，
            "缓存占用"与"LRU 判定"两套数字自相矛盾。
        以文件 mtime 作为初始 LRU 顺序，与磁盘真实状态对齐。
        """
        total = 0
        order: dict[str, float | str] = {}
        try:
            for f in self.cache_dir.glob("*.*"):
                try:
                    stat = f.stat()
                except OSError:
                    continue
                total += stat.st_size
                order[f.stem] = stat.st_mtime
                # 持久化格式元数据（与 set() 中的 `.fmt` 键规则一致），
                # 供 _evict_if_needed 构造正确后缀而非硬编码 .png
                order[f"{f.stem}.fmt"] = f.suffix.lstrip(".")
        except OSError:
            return
        with self._lock:
            self._access_order = order
            self._stats["total_size"] = total

    @staticmethod
    def _cache_key(chart_type: str, mermaid_code: str,
                   allow_pil: bool | None = None,
                   skip_http: bool = False) -> str:
        """缓存 key = chart_type + payload (+ allow_pil) (+ skip_http)。

        ✅ BUG 修复：get()/set() 与 get_or_render() 此前各用一套 key 规则 ——
        get_or_render 把 `pil=0/1` 拼进 key（避免 PIL 降级图污染"不降级"轨道），
        而公开的 get() 完全没有这一段，导致 **get() 永远读不到 get_or_render
        写入的缓存**（反向亦然），两者事实上分属两个互不可见的命名空间。
        现统一到本方法。`allow_pil=None` 表示不拼入该段，兼容直接 set/get 的
        旧调用方（该组合自成一套 key，不会被 get_or_render 命中）。

        ✅ BUG 修复：skip_http 此前未拼入 key。skip_http=False 的 HTTP 原生图
        与 skip_http=True 的 PIL 自绘图共用同一缓存槽，先调用者写入的结果会被
        后调用者直接命中（跨渲染轨道污染）。现显式拼入 `http=0/1` 段。
        """
        material = f"{chart_type}:{mermaid_code}"
        # ✅ 修复（2026-09-20 深度审查）：渲染器版本必须纳入缓存键。
        #    旧实现缓存键不含版本 —— 导出器版本升级仅使 DOCX
        #    内容指纹变化（重建文档），但图表仍从磁盘缓存返回旧
        #    PNG（mermaid 引擎 / 渲染逻辑修复后不生效）。
        material += f":v{_RENDERER_VERSION}"
        if allow_pil is not None:
            material += f":pil={int(bool(allow_pil))}"
        material += f":http={int(not bool(skip_http))}"
        return hashlib.md5(material.encode()).hexdigest()

    def get(self, mermaid_code: str, fmt: str = "png", chart_type: str = "",
            allow_pil: bool | None = None) -> Path | None:
        """获取缓存的图表

        Args:
            mermaid_code: Mermaid 代码或 JSON 数据字符串
            fmt: 图片格式（png/svg）
            chart_type: 图表类型（拼入缓存 key，避免同 payload 不同类型互踩）
            allow_pil: 与 get_or_render 对齐的降级开关段；传 True/False 才能读到
                       get_or_render 写入的缓存，None（默认）为独立命名空间

        Returns:
            缓存文件路径，未命中返回 None
        """
        # BUG修复：原 key 只含 payload MD5，同一 payload 以不同 chart_type 渲染会互踩缓存
        cache_key = self._cache_key(chart_type, mermaid_code, allow_pil)
        cache_path = self.cache_dir / f"{cache_key}.{fmt}"
        if cache_path.exists():
            with self._lock:
                self._stats["hits"] += 1
                # BUG-FIX-49 修复：get() 命中时未刷新 _access_order 时间戳，
                # 导致频繁访问的热点缓存被 LRU 误判为"最久未访问"而错误淘汰。
                import time as _time
                self._access_order[cache_key] = _time.time()
                return cache_path
        # BUG 修复：misses 计数器操作需在锁内，避免并发丢失更新
        with self._lock:
            self._stats["misses"] += 1
        return None

    def set(
        self, mermaid_code: str, content: bytes, fmt: str = "png",
        chart_type: str = "", allow_pil: bool | None = None,
    ) -> Path:
        """缓存图表

        Args:
            mermaid_code: Mermaid 代码或 JSON 数据字符串
            content: 图片二进制数据
            fmt: 图片格式
            chart_type: 图表类型（与 get() 的 key 规则一致）
            allow_pil: 拼入缓存 key（与 get_or_render 读取 key 一致）；
                       None 表示不拼入（兼容直接 set/get 的调用方）

        Returns:
            缓存文件路径
        """
        import time

        # ✅ key 规则统一到 _cache_key（get / set / get_or_render 三者必须一致，
        # 否则写入路径与读取路径对不上，缓存永远 miss）
        cache_key = self._cache_key(chart_type, mermaid_code, allow_pil)
        cache_path = self.cache_dir / f"{cache_key}.{fmt}"
        with self._lock:
            # V2.0 新增：LRU 淘汰 - 当总大小超限时，按访问最久顺序清理最早条目
            self._evict_if_needed(cache_key, len(content))
            # BUG 修复：覆写已存在条目时，total_size 需按新旧大小差值调整，
            # 否则 total_size 与磁盘实际大小脱节，导致 LRU 淘汰判定失真。
            if cache_key in self._access_order:
                old_size = cache_path.stat().st_size if cache_path.exists() else 0
                self._stats["total_size"] += len(content) - old_size
            else:
                self._stats["total_size"] += len(content)
            # ✅ BUG 修复：原直接 write_bytes 覆写目标文件，并发读取方（或导出
            # 进程）可能读到只写了一半的 PNG（解码失败）。改为写临时文件 +
            # os.replace 原子替换，任何时刻缓存路径上的文件都是完整的。
            _tmp = cache_path.with_name(f".{cache_key}.{os.getpid()}.tmp")
            try:
                _tmp.write_bytes(content)
                os.replace(_tmp, cache_path)
            finally:
                try:
                    if _tmp.exists():
                        _tmp.unlink()
                except OSError:
                    pass
            self._access_order[cache_key] = time.time()
            # 持久化格式元数据，供 _evict_if_needed 构造正确的文件路径（防止硬编码 .png 导致 SVG 条目找不到）
            self._access_order[f"{cache_key}.fmt"] = fmt
        logger.debug("ChartCache: 已缓存图表 %s (%s)", cache_key[:12], fmt)
        return cache_path

    def _evict_if_needed(self, new_key: str, new_size: int) -> None:
        """V2.0 新增：LRU 淘汰 - 按容量上限淘汰最久未访问的缓存条目"""
        while True:
            with self._lock:
                current_size = self._stats["total_size"] + new_size
                if current_size <= self._max_size_bytes:
                    return
                # 找到最久未访问的条目（排除当前要写入的 key 和 .fmt 元数据 key）
                oldest_key = None
                oldest_time = float("inf")
                for k, t in self._access_order.items():
                    # BUG-FIX-32 修复：排除 .fmt 元数据 key，避免误删格式信息
                    if k != new_key and not k.endswith(".fmt") and t < oldest_time:
                        oldest_time = t
                        oldest_key = k
                if oldest_key is None:
                    return
                # 删除最久未访问的缓存文件并更新状态
                # BUG 修复：原代码硬编码 `.png` 后缀，若某条目是 SVG 则 path 指向不存在的文件，
                # 且 total_size 不减，导致 LRU 判定持续偏高而误淘汰有效条目。
                oldest_fmt = self._access_order.get(oldest_key + ".fmt", "png")
                oldest_path = self.cache_dir / f"{oldest_key}.{oldest_fmt}"
                try:
                    if oldest_path.exists():
                        self._stats["total_size"] -= oldest_path.stat().st_size
                        oldest_path.unlink()
                except OSError:
                    pass
                del self._access_order[oldest_key]
                # BUG-FIX-32 修复：同步删除对应的 .fmt 元数据 key
                self._access_order.pop(f"{oldest_key}.fmt", None)

    def get_or_render(self, mermaid_code: str, chart_type: str, fmt: str = "png",
                      skip_http: bool = False, allow_pil: bool = True) -> BytesIO | None:
        """获取缓存或渲染新图表（线程安全，V1.1 新增）

        Args:
            mermaid_code: Mermaid 代码或 JSON 数据字符串
            chart_type: 图表类型
            fmt: 图片格式
            skip_http: 跳过 HTTP Service 渲染路径，直接走 PIL v2
            allow_pil: V7.0 不降级开关——False 时 Mermaid 语法载荷禁用 PIL 兜底。
                       注意：该开关拼入缓存 key，避免 allow_pil=True 时缓存的 PIL 降级图
                       在后续不降级导出中被错误命中。

        P0-2 单飞去重：缓存 miss 后若相同 key 已在渲染中，等待其完成并共享结果，
        避免预览 + 导出并发时同一张图被重复渲染（thundering herd）。

        Returns:
            BytesIO of PNG image, or None if rendering fails
        """
        # BUG-FIX-14: 缓存 key 拼入 chart_type，与 get/set 方法一致
        # 原实现仅用 mermaid_code 的 MD5，导致同一 payload 不同 chart_type 缓存互踩
        # V7.0: 拼入 allow_pil 状态，两档各存一份（PIL 兜底图与不降级结果互不污染）
        # ✅ 统一走 _cache_key，与 get/set 使用同一套 key 规则
        cache_key = self._cache_key(chart_type, mermaid_code, allow_pil, skip_http)
        cache_path = self.cache_dir / f"{cache_key}.{fmt}"
        # BUG-FIX-15: 先在锁内检查缓存，未命中则释放锁再渲染，避免锁内渲染导致并发串行化
        with self._lock:
            cached_content = None
            if cache_path.exists():
                cached_content = cache_path.read_bytes()
                self._stats["hits"] += 1
            if cached_content is not None:
                return BytesIO(cached_content)

        # P0-2 单飞去重：登记 in-flight，leader 渲染、follower 等待共享结果
        with self._inflight_lock:
            if cache_key in self._inflight:
                follower = True
                waiter = self._inflight[cache_key]
            else:
                follower = False
                self._inflight[cache_key] = threading.Event()
                waiter = None
        if follower:
            # ✅ BUG 修复：旧实现 waiter.wait() 无超时。leader 线程被强杀/解释器
            #    退出/异常未走到 finally 时，follower 永久阻塞（导出批量渲染时表现
            #    为整张图永久挂起、导出卡死）。最多等待 120s（单图总预算上限），
            #    超时后 follower 降级为自行渲染。
            _signaled = waiter.wait(timeout=120.0)
            if not _signaled:
                logger.warning(
                    "ChartCache: 等待同图渲染超时（120s），降级为自行渲染 %s",
                    cache_key[:12])
            with self._lock:
                if cache_path.exists():
                    self._stats["hits"] += 1
                    return BytesIO(cache_path.read_bytes())
            if not _signaled:
                # leader 已失联：清理残留 in-flight 登记，自行承担渲染
                with self._inflight_lock:
                    if self._inflight.get(cache_key) is waiter:
                        self._inflight.pop(cache_key, None)
            else:
                # leader 渲染失败：结果未写缓存，此处返回 None 与 leader 失败语义一致。
                # （不再依赖从未写入的 _inflight_result —— 已删除该死码）
                return None

        try:
            # 锁外渲染（render_mermaid_to_bytes 可能耗时数秒到20秒）
            result = render_mermaid_to_bytes(mermaid_code, chart_type,
                                             skip_http=skip_http, allow_pil=allow_pil)
            if result:
                content = result.getvalue()
                # 渲染完成后再加锁写入
                # BUG-FIX-50 修复：原直接 write_bytes 未调用 _evict_if_needed，
                # 也未更新 _access_order 和 _stats["total_size"]，导致缓存可能
                # 超 500MB 上限且统计失真。改为调用 set() 方法的完整逻辑。
                self.set(mermaid_code, content, fmt=fmt, chart_type=chart_type,
                         allow_pil=allow_pil)
                with self._lock:
                    self._stats["misses"] += 1
                return BytesIO(content)
            with self._lock:
                self._stats["misses"] += 1
            return None
        finally:
            # 唤醒所有 follower（成功路径已写缓存，follower 从缓存读取）
            with self._inflight_lock:
                ev = self._inflight.pop(cache_key, None)
                if ev is not None:
                    ev.set()

    def clear(self, max_age_days: int | None = None):
        """清理缓存

        Args:
            max_age_days: 仅清理超过 N 天的缓存；None 表示全部清理
        """
        if max_age_days is None:
            for f in self.cache_dir.glob("*.*"):
                try:
                    f.unlink()
                except Exception as _e:
                    logger.debug("[silent-except] mermaid_renderer.py: line 650 - %s", _e)
            # BUG-FIX-51 修复：原 clear() 删除磁盘文件但不重置 _access_order
            # 和 _stats["total_size"]，导致清空后 LRU 判定异常（total_size 仍为
            # 旧值，新缓存被错误淘汰）。必须重启进程才能恢复。
            with self._lock:
                self._access_order.clear()
                self._stats["total_size"] = 0
            logger.info("ChartCache: 已清空全部缓存")
        else:
            import time

            cutoff = time.time() - max_age_days * 86400
            count = 0
            for f in self.cache_dir.glob("*.*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        freed = f.stat().st_size
                        f.unlink()
                        count += 1
                        # BUG-FIX-51: 同步更新内存状态
                        with self._lock:
                            cache_key = f.stem
                            self._access_order.pop(cache_key, None)
                            self._access_order.pop(f"{cache_key}.fmt", None)
                            self._stats["total_size"] = max(0, self._stats["total_size"] - freed)
                except Exception as _e:
                    logger.debug("[silent-except] mermaid_renderer.py: line 676 - %s", _e)
            logger.info("ChartCache: 已清理 %d 个过期缓存（>%d天）", count, max_age_days)

    def get_stats(self) -> dict:
        """获取缓存统计信息

        Returns:
            包含 hits, misses, hit_rate, file_count, total_size 的字典
        """
        file_count = sum(1 for _ in self.cache_dir.glob("*.*"))
        total_size = sum(f.stat().st_size for f in self.cache_dir.glob("*.*") if f.is_file())
        total_requests = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total_requests * 100) if total_requests > 0 else 0.0

        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "hit_rate": f"{hit_rate:.1f}%",
            "file_count": file_count,
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }

    def cleanup_old_files(self, max_age_days: int = 7) -> int:
        """清理旧缓存文件

        Args:
            max_age_days: 清理超过 N 天的文件

        Returns:
            清理的文件数量
        """
        import time

        cutoff = time.time() - max_age_days * 86400
        count = 0
        for f in self.cache_dir.glob("*.*"):
            try:
                st = f.stat()
                if st.st_mtime < cutoff:
                    f.unlink()
                    count += 1
                    # ✅ BUG 修复：与 clear(max_age_days) 口径一致，删除磁盘文件后
                    # 同步内存 LRU 索引与 total_size；否则统计持续虚高、LRU 误淘汰。
                    with self._lock:
                        cache_key = f.stem
                        self._access_order.pop(cache_key, None)
                        self._access_order.pop(f"{cache_key}.fmt", None)
                        self._stats["total_size"] = max(
                            0, self._stats["total_size"] - st.st_size)
            except Exception as _e:
                logger.debug("[silent-except] mermaid_renderer.py: cleanup_old_files - %s", _e)
        if count > 0:
            logger.info("ChartCache: 清理了 %d 个超过 %d 天的缓存文件", count, max_age_days)
        return count

def render_mermaid(mermaid_code: str, chart_type: str = "flowchart") -> str:
    """兼容接口：返回 Mermaid 源码（不实际渲染）。
    
    完整渲染请使用 render_mermaid_to_bytes()。该函数保留仅为向后兼容。
    """
    return mermaid_code



# ============================================================
# 向后兼容 re-export（拆分前可用的所有内部符号）
# ============================================================
from .mermaid_common import (  # noqa: F401
    _build_gantt_marks,
    _draw_text_center,
    _fit_text_with_ellipsis,
    _image_font,
    _image_to_stream,
    _render_hq,
    _text_height,
    _text_width,
    _wrap_text,
)
from .mermaid_flowchart import (  # noqa: F401
    _DECISION_KEYWORDS,
    _extract_mermaid_node_label,
    _is_decision_label,
    _parse_flowchart_structure,
    _parse_mermaid_flowchart,
    _register_node_if_match,
    _render_flowchart_image_v2,
    _render_flowchart_with_edges,
    _split_mermaid_edge,
    _strip_mermaid_node_brackets,
    flowchart_json_to_mermaid,
)
from .mermaid_gantt import (  # noqa: F401
    _parse_mermaid_gantt,
    _render_gantt_image_v2,
    _render_gantt_with_dependencies,
    gantt_json_to_mermaid,
)
from .mermaid_architecture import (  # noqa: F401
    _layout_architecture_tree,
    _render_architecture_image_v2,
)
from .mermaid_comparison import (  # noqa: F401
    _parse_mermaid_pie,
    _parse_mermaid_xychart,
    _render_comparison_image_v2,
)
from .mermaid_labor import (  # noqa: F401
    _render_labor_image_v2,
)
from .mermaid_layout import (  # noqa: F401
    _render_layout_image_v2,
    _repair_layout_data,
)
from .mermaid_timeline import (  # noqa: F401
    _parse_mermaid_timeline,
    _render_timeline_image_v2,
    _repair_timeline_data,
)

_chart_cache = ChartCache()

