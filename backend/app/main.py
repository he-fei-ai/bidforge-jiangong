"""FastAPI 应用入口"""
import asyncio
import logging
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException
from contextlib import asynccontextmanager

from app.config import APP_VERSION, settings
from app.db import init_db, close_db, get_conn, get_db, read_db
from app.middleware import setup_middleware
from app.routers import (
    projects, schemes, sections, sse_handlers,
    outline_library, upload_outline, global_facts,
    compliance, export, ai_config, charts, scheme_catalog,
    knowledge, consistency_repair, review, system,
    bid_analysis, doc_pipeline, prompts,
)

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
    from app.config import LOGS_DIR as _LOGS_DIR
    from app.utils.safe_log_handler import SafeRotatingFileHandler
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _file_handler = SafeRotatingFileHandler(
        _LOGS_DIR / "backend.log",
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except Exception:  # noqa: BLE001 - 日志落盘失败绝不影响服务启动
    pass

# ✅ 2026-09-23（解析提取模块日志埋点）：把全链路关联 ID（trace/project/doc/task）
#    前缀到每条日志，使「上传→解析→提取→保存→分类→信息调用」可凭 ID 串联全链路。
#    见 app/utils/log_context.py。失败绝不影响启动。
try:
    from app.utils.log_context import install_filter
    install_filter(logging.getLogger())
except Exception:  # noqa: BLE001
    pass
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化数据库...")
    await init_db()
    logger.info("数据库初始化完成")

    # ✅ 启动即声明鉴权状态：避免"以为开了鉴权其实没开"（API_AUTH_TOKEN 为空时告警）
    try:
        from app.middleware import log_auth_configuration
        log_auth_configuration()
    except Exception as e:
        logger.warning("鉴权配置检查失败（不影响服务）: %s", e)

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
            strict=getattr(settings, "prompt_contract_fail_fast", False))
        if _contract_issues:
            logger.warning(
                "提示词变量契约存在 %d 处不一致"
                "（详见上方明细；开启 PROMPT_STRICT_VARIABLES 逐条打印）：",
                len(_contract_issues))
    except Exception as e:
        logger.warning("提示词变量契约校验失败（不影响服务）: %s", e)

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
                        (now_iso, t["id"]))
                    completed_cnt += 1
                else:
                    await conn.execute(
                        "UPDATE task_registry SET status='failed', message='服务重启，任务中断', "
                        "updated_at=? WHERE id=?",
                        (now_iso, t["id"]))
                    failed_cnt += 1
            await conn.commit()
            logger.warning("已清理 %d 个中断遗留任务（completed=%d, failed=%d）",
                           len(stale), completed_cnt, failed_cnt)

        # ✅ 启动恢复（2026-09-17）：「提取项目」中断遗留的 running 解析项必须一并清理。
        #    否则这些项会永久停在 running —— UI 永远显示「运行中」、summary.running>0、
        #    Tab 徽标永远不是绿色、必选项永远判为缺失（表现为「20 项未完成」），
        #    且进程已重启、没有任何任务能驱动它们进入终态。
        from app.routers.bid_analysis import clear_interrupted_items as _clear_ba_items
        from app.db import get_conn as _get_conn
        n_items = await _clear_ba_items(await _get_conn())
        if n_items:
            logger.warning("启动恢复：清理了 %d 个中断遗留的「提取项目」解析项", n_items)
    except Exception as e:
        logger.warning("启动恢复清理失败（不影响服务）: %s", e)

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

    yield

    # 关闭周期任务（先停审计 flush，再做最终冲刷；均须早于 close_db）。
    audit_stop.set()
    audit_flush_task.cancel()

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
        from app.utils.process_cleanup import sweep_temp
        from app.config import DATA_DIR, EXPORTS_DIR, FACT_UPLOADS_DIR
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

for r in (projects, schemes, sections, sse_handlers,
          outline_library, upload_outline, global_facts,
          compliance, export, ai_config, charts, scheme_catalog,
          knowledge, consistency_repair, review, system,
          bid_analysis, doc_pipeline, prompts):
    app.include_router(r.router)


@app.get("/api/v1/health")
async def health():
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
        "pil_renderer": False,          # 内置 PIL v2 渲染器（默认渲染路径，无需 mmdc）
        "http_service": os.environ.get("MERMAID_SERVICE_URL", ""),
        "railway": os.environ.get("MERMAID_RAILWAY_URL", ""),
    }
    try:
        import PIL  # noqa: F401
        mermaid["pil_renderer"] = True
    except Exception:
        pass
    mermaid["available"] = bool(
        mermaid["pil_renderer"] or mermaid["http_service"] or mermaid["railway"])
    mermaid["note"] = "mmdc CLI 已不需要；内置 PIL v2 渲染器覆盖全部图表类型"
    result["mermaid"] = mermaid

    # ---- 文档解析依赖 ----
    parsers: dict[str, bool] = {}
    for mod, label in (
        ("docx", "docx"), ("docx2python", "docx_fallback"),
        ("pdfplumber", "pdf"), ("pypdf", "pdf_fallback"), ("fitz", "pdf_fast"),
        ("openpyxl", "xlsx"), ("xlrd", "xls"), ("csv", "csv"),
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
    list_prompts, prompt_audit_logs, reset_prompt, rollback_prompt, update_prompt,
)

__all__ = [
    "list_prompts", "update_prompt", "reset_prompt", "prompt_audit_logs",
    "rollback_prompt",
]
