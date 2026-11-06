"""FastAPI 应用入口"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

from app.config import APP_VERSION, settings
from app.db import close_db, init_db
from app.middleware import setup_middleware
from app.routers import (
    ai_config,
    bid_analysis,
    charts,
    compliance,
    consistency_repair,
    defects,
    doc_pipeline,
    export,
    export_dossier,
    global_facts,
    knowledge,
    outline_library,
    projects,
    prompts,
    review,
    review_autofix,
    scheme_catalog,
    schemes,
    scoring,
    sections,
    sse_handlers,
    standards_admin,
    system,
    upload_outline,
)

# ✅ R102（2026-10-18 · 日志审计收口）：控制台流编码统一。
#    生产实证（backend_err.log / backend_out.log，2026-10-01 快照）：全部
#    1412 行含中文的日志均为 GBK 字节（Windows locale 默认编码），而生产
#    文件 handler（SafeRotatingFileHandler encoding="utf-8"）写出的
#    logs/backend.log 是 UTF-8 —— 同一系统两种编码，任何按 UTF-8 假设的
#    日志工具（ELK / grep -P / 本仓分析脚本）解析重定向日志即得乱码，
#    编码探测失败时还会整文件解码失败（实测 utf-8 decode 在 0xd5 处崩）。
#    修复：仅当 stdout/stderr **被重定向**（非 TTY）时 reconfigure 为
#    UTF-8 —— 重定向场景按 UTF-8 落盘供工具链解析；TTY 场景保持 locale
#    编码不变，实时控制台中文显示零回归（GBK 代码页下不会出现乱码）。
#    reconfigure 为原地修改，uvicorn handler 已持有的 stream 引用同步生效。
try:
    import sys as _sys_for_enc

    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(_sys_for_enc, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure") and not _stream.isatty():
            try:
                _stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # noqa: BLE001 - 特殊流形态不强求
                pass
except Exception:  # noqa: BLE001 - 编码调整绝不影响服务启动
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# ✅ 2026-09-21：运行日志落盘（logs/backend.log，5MB × 3 轮转）。
#    此前只有控制台输出——后台 cmd 窗口一关，调用痕迹/失败堆栈全部丢失，
#    事后无法做调用次数与失败归因分析（logs/ 目录在编译计划书中规划但从未接线）。
# ✅ 2026-09-23 事故加固：标准库 RotatingFileHandler 在轮转失败（典型：句柄被
#    dev server 运行期间跑的 pytest 进程占用）时会**丢弃所有后续日志记录**，
#    实测日志冻结 7 小时。改用 SafeRotatingFileHandler：轮转失败降级为追加写。
try:
    # ✅ 2026-10-07：pytest 进程不挂生产文件 handler。
    #    大量 API 测试会 import app.main，此前测试噪声被写进 logs/backend.log
    #    —— 生产日志与测试日志混在一起，P95/P99、ERROR Top、HTTP 端点延迟等
    #    排障口径全部失真（唯一日志源被污染，且轮转配额被测试日志消耗）。
    #    生产进程不受影响（无 pytest 模块、无 PYTEST_CURRENT_TEST）。
    # ✅ R108（2026-10-18 · 日志审计 P2-1 · 污染面闭合）：上述 pytest 守卫
    #    只挡得住 pytest 进程；生产实证 backend.log 2026-10-18 11:32 仍有
    #    "TestClient" 请求行——**非 pytest 的独立验证脚本**（artifacts 下
    #    ab_/e2e 类，直接 `from fastapi.testclient import TestClient`）
    #    绕过守卫把测试流量写进生产日志。修法两层：
    #    ① 守卫增加 starlette.testclient 模块检测（用 TestClient 必先 import
    #       它，比 argv/环境变量判定可靠；uvicorn 生产进程永不出现）；
    #    ② 安装点从「仅模块级」扩为「模块级 + lifespan 首行」双点幂等——
    #       脚本先 import app.main 再 import TestClient 时模块级守卫拦不住，
    #       但 lifespan（TestClient with 上下文触发）此刻 starlette.testclient
    #       必已在 sys.modules，第二道闸 100% 生效。
    #    逃生开关 ALLOW_FILE_LOG_IN_TESTCLIENT=1 供调试脚本主动落盘。
    import os as _os
    import sys as _sys

    from app.config import LOGS_DIR as _LOGS_DIR
    from app.utils.safe_log_handler import SafeRotatingFileHandler

    _file_log_handler_installed = False

    def _should_skip_file_log() -> bool:
        """测试/验证进程返回 True（不挂生产文件 handler）。"""
        if _os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in _sys.modules:
            return True
        if "starlette.testclient" in _sys.modules:
            if _os.environ.get("ALLOW_FILE_LOG_IN_TESTCLIENT") == "1":
                return False
            return True
        return False

    def _install_production_file_log_handler() -> None:
        """挂 logs/backend.log 轮转 handler（幂等，测试进程跳过）。

        ✅ R108：SafeRotatingFileHandler 轮转失败降级追加写（2026-09-23 事故）；
        安装点双闸见上方注释。任何异常绝不影响服务启动（外层 try 兜底）。
        """
        global _file_log_handler_installed
        if _file_log_handler_installed or _should_skip_file_log():
            return
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        _fh = SafeRotatingFileHandler(
            _LOGS_DIR / "backend.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(_fh)
        _file_log_handler_installed = True

    _install_production_file_log_handler()
except Exception:  # noqa: BLE001 - 日志落盘失败绝不影响服务启动
    pass

# ✅ 2026-09-23（解析提取模块日志埋点）：把全链路关联 ID（trace/project/doc/task）
#    前缀到每条日志，使「上传→解析→提取→保存→分类→信息调用」可凭 ID 串联全链路。
#    见 app/utils/log_context.py。失败绝不影响启动。
try:
    from app.utils.log_context import install_filter

    install_filter(logging.getLogger())
    # ✅ R102（2026-10-18 · 日志审计收口）：uvicorn 自带 handler 的
    #    `uvicorn` / `uvicorn.access` 两个 logger 是 propagate=false + 自有
    #    handler → 访问日志**不过** root 的 TraceContextFilter，访问行恒缺
    #    [trace=…]。生产实证（backend_err.log 2026-10-01）：1952 条请求中
    #    613 条 /health 只以无 trace 的 uvicorn 访问行存在，grep trace 串链
    #    时这部分请求凭空消失（埋点缺口）。uvicorn 的 logging 配置在其
    #    Config.__init__ 阶段完成、早于 app import，故此处补挂 filter 生效
    #    且不会被覆盖（幂等，重复 import 不重复挂）。
    for _uv_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        try:
            install_filter(logging.getLogger(_uv_name))
        except Exception:  # noqa: BLE001 - 单个 logger 挂载失败不影响其它
            pass
except Exception:  # noqa: BLE001 - trace 过滤器挂载失败只影响日志可串联性，绝不影响服务启动
    pass
logger = logging.getLogger("main")


async def _run_startup_orphan_export_gc() -> None:
    """启动期：对每个在 export_cache 里有行的 scheme 跑一次孤儿导出产物 GC。

    R47 债-A3：``_gc_orphan_exports`` 只在导出时跑；长期不导出的方案其
    孤儿产物（.tmp.docx / PDF INSERT 失败的成稿）永不被清。启动时一次性
    扫全表兜底。fail-soft：单方案失败仅告警，整体异常由 lifespan 外层
    try/except 兜住，绝不阻断启动。
    """
    from app.db import get_conn as _gc_get_conn
    from app.routers.export import _gc_orphan_exports as _gc_orphans

    _gc_db = await _gc_get_conn()
    _rows = await _gc_db.execute("SELECT DISTINCT scheme_id FROM export_cache")
    _schemes = [r[0] for r in await _rows.fetchall()]
    for _sid in _schemes:
        if not _sid:
            continue
        try:
            await _gc_orphans(_gc_db, _sid, protect=None)
        except Exception as _e:  # 单方案失败不影响其它方案与启动
            logger.warning("启动期孤儿导出 GC 失败（scheme=%s）: %s", str(_sid)[:8], _e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ✅ R108（P2-1）：第二道安装点——脚本先 import app.main 后 import
    #    TestClient 时模块级守卫漏拦，但走到这里 starlette.testclient 必已
    #    在 sys.modules（TestClient with 上下文才触发 lifespan），闸 100% 生效；
    #    生产 uvicorn / --reload 子进程同样走 lifespan，正常挂 handler（幂等）。
    try:
        _install_production_file_log_handler()
    except Exception:  # noqa: BLE001 - 日志落盘失败绝不影响服务启动
        pass
    logger.info("正在初始化数据库...")
    await init_db()
    logger.info("数据库初始化完成")

    # ✅ 启动即声明鉴权状态：避免"以为开了鉴权其实没开"（API_AUTH_TOKEN 为空时告警）
    try:
        from app.middleware import log_auth_configuration

        log_auth_configuration()
    except Exception as e:
        logger.warning("鉴权配置检查失败（不影响服务）: %s", e)

    # ✅ R175（BUG-06）：主密钥启动期自检 —— 空密钥文件 / 不可写目录不再静默
    #    降级为进程级临时密钥（那会让重启后历史密文永久不可解）。fail-soft。
    try:
        from app.services.crypto import ensure_master_key_ready

        ensure_master_key_ready()
    except Exception as e:
        logger.warning("主密钥启动自检失败（不影响服务）: %s", e)

    # ✅ G4 变量契约启动期校验（2026-09-24）：把「缺变量只能靠运行期日志发现」
    #    提前到启动期。检查声明了 requires 的模板与其实际占位符是否双向一致。
    # ✅ 2026-09-25（BUG-B · 配置项静默失效修复）：
    #   旧实现 ``verbose=settings.prompt_strict_variables`` —— verbose=False 时
    #   check_prompt_variables 完全不打日志，导致默认配置下契约漂移**静默无声**，
    #   且 prompt_strict_variables 从未真正「开启校验」，纯属配置项静默失效。
    #   现拆成两个语义明确的路径：
    #     * 漂移明细：verbose=prompt_strict_variables 控制是否逐条打印（详细排查用）；
    #     * 汇总告警：无论 verbose 取值都打一条（默认配置也能发现漂移）；
    #     * 严格模式：prompt_contract_fail_fast=True 时阻断启动（默认关闭，
    #       向后兼容；开启后契约漂移 = 部署阻断，适合 CI / 生产门禁）。
    try:
        from app.services.ai.prompts import check_prompt_variables

        _contract_issues = check_prompt_variables(
            verbose=getattr(settings, "prompt_strict_variables", False),
            strict=getattr(settings, "prompt_contract_fail_fast", False),
        )
        if _contract_issues:
            logger.warning(
                "提示词变量契约存在 %d 处不一致" "（详见上方明细；开启 PROMPT_STRICT_VARIABLES 逐条打印）：",
                len(_contract_issues),
            )
    except Exception as e:
        logger.warning("提示词变量契约校验失败（不影响服务）: %s", e)

    # ✅ R175（2026-11-03 · BUG-2 修复）：从运行时设置表回放「提示词治理开关」
    #    旧实现 PUT /system/governance 只写内存，重启即回 .env 默认 ——
    #    用户开启的注入防护/凭据脱敏会**静默关闭**。现在启动期以库为准回填，
    #    并打印一次治理姿态（脱敏未开启时给出明确风险告警）。
    try:
        from app.routers.system import load_governance_settings

        await load_governance_settings()
        from app.services.prompt_material_guard import log_governance_posture_once

        log_governance_posture_once()
    except Exception as e:  # noqa: BLE001 - 治理开关回放失败不影响服务启动
        logger.warning("提示词治理开关回放失败（保留默认值）: %s", e)

    # ✅ 启动恢复：把上次进程中断遗留的 running/paused 僵尸任务按进度分级处理
    try:
        from app.services.ai.task_registry import get_interrupted_tasks

        stale = await get_interrupted_tasks()
        if stale:
            from app.db import get_conn

            conn = await get_conn()
            now_iso = datetime.now().isoformat()
            completed_cnt = 0
            failed_cnt = 0
            for t in stale:
                p = float(t.get("progress") or 0)
                if p >= 0.95:
                    # 进度已满：视为已完成（后端 finish_task 可能在进程退出前没来得及更新）
                    await conn.execute(
                        "UPDATE task_registry SET status='completed', message=message || '（兜底：启动恢复判定 progress 已满）', "
                        "updated_at=? WHERE id=?",
                        (now_iso, t["id"]),
                    )
                    completed_cnt += 1
                else:
                    await conn.execute(
                        "UPDATE task_registry SET status='failed', message='服务重启，任务中断', "
                        "updated_at=? WHERE id=?",
                        (now_iso, t["id"]),
                    )
                    failed_cnt += 1
            await conn.commit()
            logger.warning("已清理 %d 个中断遗留任务（completed=%d, failed=%d）", len(stale), completed_cnt, failed_cnt)

        # ✅ 启动恢复（2026-09-17）：「提取项目」中断遗留的 running 解析项必须一并清理。
        #    否则这些项会永久停在 running —— UI 永远显示「运行中」、summary.running>0、
        #    Tab 徽标永远不是绿色、必选项永远判为缺失（表现为「20 项未完成」），
        #    且进程已重启、没有任何任务能驱动它们进入终态。
        from app.db import get_conn as _get_conn
        from app.routers.bid_analysis import clear_interrupted_items as _clear_ba_items

        n_items = await _clear_ba_items(await _get_conn())
        if n_items:
            logger.warning("启动恢复：清理了 %d 个中断遗留的「提取项目」解析项", n_items)
    except Exception as e:
        logger.warning("启动恢复清理失败（不影响服务）: %s", e)

    # ✅ R47 债-A3（2026-10-06）：启动期孤儿导出产物 GC。
    #    _gc_orphan_exports 只在导出时跑；长期不导出的方案其孤儿产物
    #    （.tmp.docx / PDF INSERT 失败的成稿）永不被清。启动时对每个在
    #    export_cache 里有行的 scheme 跑一次孤儿回收。fail-soft：任何异常
    #    只记 WARNING，绝不阻断启动。
    try:
        await _run_startup_orphan_export_gc()
    except Exception as e:
        logger.warning("启动期孤儿导出 GC 跳过（不影响服务）: %s", e)

    # ✅ 统一并发体系：启动时应用活跃 AI 配置的并发数作为全局默认并发
    try:
        from app.services.ai.provider_factory import apply_config_concurrency

        await apply_config_concurrency()
    except Exception as e:
        logger.warning("启动应用配置并发失败（不影响服务）: %s", e)

    # ✅ 关闭自动清理（僵尸进程/临时文件治理，2026-09-23）：登记 atexit 兜底钩子。
    #    lifespan 关闭段是主路径；atexit 是解释器正常退出时的第二道保险（幂等，
    #    两处都只做 best-effort 清理）。默认不抢占 SIGINT/SIGTERM —— uvicorn 自管
    #    优雅关闭，抢占信号反而可能打断 lifespan。见 app/utils/process_cleanup.py。
    try:
        from app.utils.process_cleanup import register_shutdown_cleanup, sweep_temp

        def _app_temp_sweep():
            from app.config import DATA_DIR, EXPORTS_DIR, FACT_UPLOADS_DIR

            return sweep_temp(
                [DATA_DIR, FACT_UPLOADS_DIR, EXPORTS_DIR],
                patterns=("office-convert-*.ps1", "*.lock", "~$*"),
            )

        register_shutdown_cleanup(_app_temp_sweep)
    except Exception as e:
        logger.warning("登记关闭清理钩子失败（不影响服务）: %s", e)

    # ✅ 可靠性统计预热（2026-09-17）：从审计表恢复最近 24h 的 Provider 成败统计，
    #    重启后死配置（如 100% 失败的候选）在第一次调用前就会被降级链剔除，
    #    避免每轮重启都白等一次网络往返。
    try:
        from app.services.ai.provider_factory import warmup_reliability_from_db

        await warmup_reliability_from_db()
    except Exception as e:
        logger.warning("预热 Provider 可靠性统计失败（不影响服务）: %s", e)

    # ✅ R103（2026-10-18 · 日志审计遗留 L1 收口）：预热之后立刻把
    #    「近 24h 实时成功率过低、一上来就会被剔除出降级链」的死配置身份
    #    逐条 WARNING。此前该判定只发生在每次选路的内存里、无可观测输出——
    #    生产实证（R102 基线）：4 条 nvidia 死配置（HTTP 404
    #    UnsupportedModel）+ volcengine 不支持 coding plan 的模型长期占据
    #    候选池，用户端只见「降级链 9→3」不知该清理谁。**只读日志、
    #    零行为变更**（不剔除/不禁用/不写库）。fail-soft。
    try:
        from app.services.ai.provider_factory import report_dead_configs_at_startup

        await report_dead_configs_at_startup()
    except Exception as e:
        logger.warning("启动死配置报告失败（不影响服务）: %s", e)

    # ✅ R179 U5（2026-11-05）：分组预热编排配置自检 —— 只读配置、不发起 AI 调用。
    #    开关关闭 → status=disabled/equivalent=verbatim（逐字现状）；
    #    开关打开但阶段上限=1 → WARNING「等于未开启」（enabled_but_no_gain）。
    try:
        from app.services.ai.warmup import log_config_selfcheck

        log_config_selfcheck()
    except Exception as e:
        logger.warning("U5 预热配置自检失败（不影响服务）: %s", e)

    # ✅ 2026-10-05：Provider 运行时状态预热（配额冷却 / 熔断）——
    #    补齐「重启即遗忘」缺口：cool_until 与 circuit breaker state 是
    #    进程内 dict，重启发生在冷却/熔断窗口内时会「蒸发」；启动时
    #    从 ai_provider_state 恢复仍在窗口内的行，避免下次调用又白烧一次 429。
    try:
        from app.services.ai.provider_factory import warmup_provider_state_from_db

        await warmup_provider_state_from_db()
    except Exception as e:
        logger.warning("预热 Provider 运行时状态失败（不影响服务）: %s", e)

    # ✅ P0 断连修复（2026-09-23）· 运行期僵尸任务周期回收：启动清理只覆盖
    #    「进程重启」场景；SSE 断连/流异常残留的「DB running、内存无态」僵尸
    #    若不重启进程会永远转圈（任务栏清不掉）。每 5 分钟扫一次，把超过
    #    15 分钟无任何进度且本进程无运行实例的任务落终态 stopped。
    async def _reap_orphan_loop():
        from app.services.ai.task_registry import reap_orphan_tasks

        while True:
            await asyncio.sleep(300)
            try:
                n = await reap_orphan_tasks(max_stale_seconds=900)
                if n:
                    logger.warning("周期回收僵尸任务 %d 个（DB running 但无运行实例）", n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("周期回收僵尸任务失败（不影响服务）: %s", e)

    reaper_task = asyncio.create_task(_reap_orphan_loop())

    # AI 审计攒批必须有真实时钟触发：旧实现只在「下一条审计到来」时检查 10s，
    # 单条记录后若没有后续调用会一直滞留到进程退出。默认行为不变，仅补准时落库。
    from app.services.ai.provider_factory import audit_flush_loop

    audit_stop = asyncio.Event()
    audit_flush_task = asyncio.create_task(audit_flush_loop(audit_stop))

    # ✅ 2026-10-05：Provider 运行时状态周期落库（配额冷却 / 熔断）。
    #    与 warmup_provider_state_from_db 配对——启动读、周期写，
    #    重启时能把仍在冷却/熔断窗口内的行原样恢复（避免「蒸发」）。
    #    失败仅告警，下一周期重试；不影响审计 flush / 主流程。
    from app.services.ai.provider_factory import _persist_provider_state_loop

    state_stop = asyncio.Event()
    state_flush_task = asyncio.create_task(_persist_provider_state_loop(state_stop))

    yield

    # 关闭周期任务（先停审计 flush，再做最终冲刷；均须早于 close_db）。
    audit_stop.set()
    audit_flush_task.cancel()

    # 关闭 Provider 状态周期落库任务；先停循环再做一次最终快照写入
    # （避免最后一次状态变更在周期任务停止后才发生）。
    state_stop.set()
    state_flush_task.cancel()
    try:
        await state_flush_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("停止 Provider 状态周期落库任务失败（不影响关闭）: %s", e)
    try:
        from app.services.ai.provider_factory import flush_provider_state_to_db

        await flush_provider_state_to_db()
    except Exception as e:
        logger.warning("关闭前 Provider 状态最终落库失败（不影响关闭）: %s", e)

    # 关闭周期回收任务（必须先于 close_db，避免回收写入撞上已关闭的连接）
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("停止周期回收任务失败（不影响关闭）: %s", e)

    # 关闭前冲刷 AI 审计日志缓冲：周期任务负责运行期，最终冲刷负责收尾。
    try:
        await audit_flush_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("停止 AI 审计周期任务失败（不影响关闭）: %s", e)
    try:
        from app.services.ai.provider_factory import flush_audit_buffer

        await flush_audit_buffer()
    except Exception as e:
        logger.warning("冲刷 AI 审计日志缓冲失败（不影响关闭）: %s", e)

    # ✅ 关闭 AI Provider 的共享 HTTP 连接池（进程级复用，见 http_pool.py）
    try:
        from app.services.ai.http_pool import aclose_all_clients

        await aclose_all_clients()
    except Exception as e:
        logger.warning("关闭 AI HTTP 连接池失败（不影响关闭）: %s", e)

    logger.info("正在关闭数据库...")
    await close_db()

    # ✅ 关闭即时清理：回收应用自有的临时转换脚本 / 锁文件（best-effort，失败仅告警）。
    #    数据库已关，此时不会误删在用文件；atexit 钩子作为未执行到本行的兵补。
    try:
        from app.config import DATA_DIR, EXPORTS_DIR, FACT_UPLOADS_DIR
        from app.utils.process_cleanup import sweep_temp

        sweep_temp(
            [DATA_DIR, FACT_UPLOADS_DIR, EXPORTS_DIR],
            patterns=("office-convert-*.ps1", "*.lock", "~$*"),
        )
    except Exception as e:
        logger.warning("关闭即时清理失败（不影响关闭）: %s", e)


app = FastAPI(
    title="工程项目专项方案智能编制平台",
    version=APP_VERSION,
    lifespan=lifespan,
)

setup_middleware(app)

for r in (
    projects,
    schemes,
    sections,
    sse_handlers,
    outline_library,
    upload_outline,
    global_facts,
    compliance,
    export,
    export_dossier,
    defects,
    standards_admin,
    ai_config,
    charts,
    scheme_catalog,
    knowledge,
    consistency_repair,
    review,
    review_autofix,
    system,
    bid_analysis,
    doc_pipeline,
    prompts,
    scoring,
):
    app.include_router(r.router)

# ✅ 2026-10-09 拆分：global_facts_docs 文档管理子路由（同 prefix）
from app.routers import global_facts_docs as _gfd

app.include_router(_gfd.router)

# ✅ R48（2026-10-06）：prompts 运行时指标 + 硬编码→DB 一键同步。
#    这两个路由挂在独立前缀（/system、/admin/prompts），不在上面的
#    /api/v1/prompts 命名空间下，故在此单独注册。
app.include_router(prompts.system_metrics_router)
app.include_router(prompts.admin_prompts_router)


@app.get("/api/v1/health")
async def health():
    # ✅ B1-4（R176 · 2026-11-04）：`_migrate` 的非致命失败不再只停留在日志里 ——
    #    迁移降级状态在此显式暴露（status=degraded + 明细）。无失败时返回体
    #    与引入前**逐字一致**（status=ok），既有探活/网关零影响。
    from app.db import migration_status, schema_status

    mig = migration_status()
    sch = schema_status()
    if mig["degraded"] or not sch["ok"]:
        # ✅ R181（N-10）：结构不一致（缺表/缺列/版本不匹配）与迁移降级同口径暴露，
        #    旧前端只读 status/version，新增字段为加法式扩展，不影响既有探活。
        return {"status": "degraded", "version": APP_VERSION, "migrations": mig, "schema": sch}
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/v1/diagnostics/capabilities")
async def capabilities():
    """能力自检：OCR / 图表渲染 / 文档解析依赖是否就绪。

    用于快速定位「扫描件无法提取」「导出无图」这类环境依赖问题：
    返回各引擎是否可用，以及不可用时的启用方法。
    """
    result: dict = {"version": APP_VERSION}

    # ---- OCR（扫描件 / 图片型资料）----
    try:
        from app.services.ocr import ocr_capabilities_async

        result["ocr"] = await ocr_capabilities_async()
    except Exception as e:
        result["ocr"] = {"available": False, "error": str(e)}

    # ---- 图表渲染（Mermaid：PIL v2 内置渲染器 / 可选 HTTP 服务）----
    import os

    mermaid = {
        "pil_renderer": False,  # 内置 PIL v2 渲染器（默认渲染路径，无需 mmdc）
        "http_service": os.environ.get("MERMAID_SERVICE_URL", ""),
        "railway": os.environ.get("MERMAID_RAILWAY_URL", ""),
    }
    try:
        import PIL  # noqa: F401

        mermaid["pil_renderer"] = True
    except Exception:  # noqa: BLE001 - 可选依赖探测，结果经响应体 pil_renderer 外显，日志反成轮询噪声
        pass
    mermaid["available"] = bool(mermaid["pil_renderer"] or mermaid["http_service"] or mermaid["railway"])
    mermaid["note"] = "mmdc CLI 已不需要；内置 PIL v2 渲染器覆盖全部图表类型"
    result["mermaid"] = mermaid

    # ---- 文档解析依赖 ----
    parsers: dict[str, bool] = {}
    for mod, label in (
        ("docx", "docx"),
        ("docx2python", "docx_fallback"),
        ("pdfplumber", "pdf"),
        ("pypdf", "pdf_fallback"),
        ("fitz", "pdf_fast"),
        ("openpyxl", "xlsx"),
        ("xlrd", "xls"),
        ("csv", "csv"),
    ):
        try:
            __import__(mod)
            parsers[label] = True
        except Exception:
            parsers[label] = False
    result["parsers"] = parsers
    return result


# 提示词路由已拆到 app.routers.prompts；为兼容旧调用与现有测试仍保留 re-export。
from app.routers.prompts import (
    list_prompts,
    prompt_audit_logs,
    reset_prompt,
    rollback_prompt,
    update_prompt,
)

__all__ = [
    "list_prompts",
    "update_prompt",
    "reset_prompt",
    "prompt_audit_logs",
    "rollback_prompt",
]
