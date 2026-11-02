# -*- coding: utf-8 -*-
"""两轮次导出 · 任务化路由（R147）。

第一轮（初稿）完整复用 export.py 的既有链路
（_prepare_export → _prepare_export_render → _build_docx_sync）；
第二轮（检查修复）走 services/docx_repair/pipeline.run_two_round_repair。

路由（全部新增，旧 /docx 零改动）：
- POST /api/v1/schemes/{sid}/export/docx/v2      → 202 {task_id, ...}
- GET  /api/v1/schemes/{sid}/export/flow/{tid}/events   （SSE：两段进度 + 修复项实时流）
- GET  /api/v1/schemes/{sid}/export/flow/{tid}/download （终稿 / 回滚初稿下载）
- POST /api/v1/schemes/{sid}/export/flow/{tid}/cancel   （真停止）

状态与日志持久化：task_registry（status/progress/message/checkpoint_json，
零 DDL）；修复报告整体写入 checkpoint_json，SSE 事件另存内存环形缓冲
（断线重挂接回放）。回滚契约：第二轮任何未捕获异常 → 删除半成品 →
初稿原子改名为交付物（mode=fallback_draft），任务终态 degraded。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.config import LOGS_DIR
from app.db import get_conn, safe_rowcount
from app.routers.export import (
    _DOCX_MIME,
    EXPORTS_DIR,
    ExportCancelled,
    _build_docx_sync,
    _build_docx_task,
    _build_export_filename,
    _bump_export_round,
    _content_fingerprint,
    _display_scheme_name,
    _export_name_headers,
    _finalize_export_delivery,
    _persist_export_cache_row,
    _prepare_export,
    _prepare_export_render,
)
from app.services.ai import task_registry as _tr
from app.services.docx_repair.pipeline import run_two_round_repair

logger = logging.getLogger("export_flow")

router = APIRouter(prefix="/api/v1/schemes", tags=["export-flow"])

#: task_registry 任务类型（与目录/正文任务同表，防僵尸/互斥逻辑同源）
TASK_TYPE = "export_docx_flow"

#: 内存流状态：task_id → {buffer, waiters, terminal, report, final_path, filename, mode}
_FLOWS: dict[str, dict] = {}
_FLOW_LOCK = asyncio.Lock()
_BUFFER_MAX = 4000  # 事件环形缓冲上限（防异常初稿撑爆内存）


# ---------------------------------------------------------------------------
# 事件通道
# ---------------------------------------------------------------------------

def _state(task_id: str) -> dict:
    return _FLOWS.setdefault(task_id, {
        "buffer": [], "waiters": set(), "terminal": False,
        "report": None, "final_path": None, "filename": None, "mode": None,
        "sink": None,
    })


async def _emit(task_id: str, evt: dict) -> None:
    st = _state(task_id)
    evt = dict(evt)
    evt["ts"] = datetime.now().isoformat(timespec="seconds")
    if evt.get("event") == "terminal" or evt.get("terminal"):
        st["terminal"] = True
    sink = st.get("sink")
    if sink:
        try:
            sink(evt)  # jsonl 留痕（backend/logs/docx_repair/<task_id>.jsonl）
        except Exception:  # noqa: BLE001 —— 留痕失败不影响交付
            pass
    st["buffer"].append(evt)
    if len(st["buffer"]) > _BUFFER_MAX:
        del st["buffer"][:len(st["buffer"]) - _BUFFER_MAX]
    for q in list(st["waiters"]):
        try:
            q.put_nowait(evt)
        except Exception:  # noqa: BLE001 —— 单个订阅者异常不影响主流程
            pass


def _thread_emit(task_id: str, loop: asyncio.AbstractEventLoop):
    """构建/修复线程内的 emit 回调 → 桥接回事件循环。"""

    def _cb(evt: dict) -> None:
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_emit(task_id, dict(evt))))
        except RuntimeError:
            pass  # 循环已关（进程退出/任务取消）：静默丢弃
    return _cb


def _jsonl_sink(task_id: str):
    """修复过程留痕（双留痕之一）：逐事件追加 jsonl，文件级可审计。"""
    path = Path(LOGS_DIR) / "docx_repair" / f"{task_id}.jsonl"
    lock = threading.Lock()

    def _write(evt: dict) -> None:
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(evt, ensure_ascii=False)[:20000] + "\n")
    return _write


def _write_draft_manifest(task_id: str, *, scheme_id: str, prep: dict,
                          fix_stats: dict, draft_path: Path,
                          manifest_path: Path) -> dict:
    """第一轮落盘清单：初稿的构成与已知缺陷（第二轮比对的基线）。"""
    chart_lookup = prep.get("chart_lookup") or {}
    rendered = prep.get("rendered_bytes") or {}
    skipped_charts = len(set(chart_lookup) - set(rendered))
    manifest = {
        "task_id": task_id,
        "scheme_id": scheme_id,
        "draft": str(draft_path),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "size_bytes": draft_path.stat().st_size if draft_path.exists() else 0,
        "font_name": (prep.get("docx_options") or {}).get("font_name") or "宋体",
        "heading_font": ((prep.get("heading_styles") or {}).get(1) or {}).get(
            "font_name") or "黑体",
        "expected_outline": _expected_outline_from_prep(prep),
        "chart_skipped": skipped_charts,
        "markdown_residue": int(
            (fix_stats or {}).get("postcheck_markdown_residue") or 0),
        "round1_stats": dict(fix_stats or {}),
    }
    try:
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("初稿清单写入失败（不阻断）: %s", e)
    return manifest


# ---------------------------------------------------------------------------
# 第一轮 + 第二轮主体（后台协程）
# ---------------------------------------------------------------------------

async def _run_flow(task_id: str, scheme_id: str, body: dict) -> None:
    st = _state(task_id)
    cancel_ev = threading.Event()

    async def _stop_watcher():
        # task_registry 停止指令 → 构建线程取消事件（200ms 级生效）
        while not st["terminal"]:
            if _tr.is_stopped(task_id):
                cancel_ev.set()
                return
            await asyncio.sleep(0.2)

    def _check_cancelled():
        # 段边界协作式退出（构建线程内部另有检查点）
        if cancel_ev.is_set():
            raise ExportCancelled()

    st["sink"] = _jsonl_sink(task_id)
    watcher = asyncio.create_task(_stop_watcher())
    db = None
    draft_path: Path | None = None
    final_path: Path | None = None
    manifest_path: Path | None = None
    delivered = None
    mode = "fixed"
    report: dict | None = None
    loop = asyncio.get_running_loop()
    try:
        db = await get_conn()
        await _tr.update_progress(task_id, 2.0, "准备导出数据（第一轮·初稿）")
        await _emit(task_id, {"stage": "draft", "phase": "prepare", "progress": 2.0,
                              "message": "准备导出数据（第一轮·初稿）"})

        # ===== 第一轮 · 初稿（0 → 40）=====
        prep = await _prepare_export(scheme_id, body, db, cancel_event=cancel_ev)
        _check_cancelled()
        await _tr.update_progress(task_id, 15.0, "取数完成，渲染图表")
        await _emit(task_id, {"stage": "draft", "phase": "render", "progress": 15.0,
                              "message": "取数完成，渲染图表"})
        await _prepare_export_render(prep)
        _check_cancelled()
        await _tr.update_progress(task_id, 30.0, "图表渲染完成，组装文档")
        await _emit(task_id, {"stage": "draft", "phase": "build", "progress": 30.0,
                              "message": "图表渲染完成，组装文档初稿"})

        config_hash, content_hash = _content_fingerprint(prep)
        round_no = await _bump_export_round(db, scheme_id)
        scheme = prep["scheme"]
        export_filename = _build_export_filename(
            _display_scheme_name(scheme, prep.get("global_facts")), "docx", round_no)
        draft_path = EXPORTS_DIR / f"{task_id}.draft.docx"
        final_path = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{content_hash[:8]}.docx"

        fix_stats = await asyncio.to_thread(
            _build_docx_sync, *_build_docx_task(str(draft_path), prep))
        _check_cancelled()
        manifest_path = EXPORTS_DIR / f"{task_id}.draft.manifest.json"
        manifest = _write_draft_manifest(
            task_id, scheme_id=scheme_id, prep=prep, fix_stats=fix_stats or {},
            draft_path=draft_path, manifest_path=manifest_path)
        await _tr.update_progress(task_id, 40.0, "初稿完成，开始全面检查与修复（第二轮）")
        await _emit(task_id, {"stage": "draft", "phase": "done", "progress": 40.0,
                              "message": "初稿完成，开始全面检查与修复（第二轮）",
                              "round1_stats": dict(fix_stats or {}),
                              "manifest": manifest})

        # ===== 第二轮 · 全面检查与修复（40 → 90）=====
        report = await asyncio.to_thread(
            run_two_round_repair,
            str(draft_path), str(final_path),
            expected_outline=_expected_outline_from_prep(prep),
            media_sources=None,
            east_asia_font=manifest["font_name"],
            heading_font=manifest["heading_font"],
            emit=_thread_emit(task_id, loop),
            round1_stats=dict(fix_stats or {}),
        )
        mode = str(report.get("mode") or "fixed")
        delivered = final_path

        # ===== 打包与兼容性校验（90 → 100）=====
        await _tr.update_progress(task_id, 96.0, "打包与兼容性校验")
        await _emit(task_id, {"stage": "packaging", "phase": "finalize",
                              "progress": 96.0,
                              "message": "打包与兼容性校验（zip/命名空间/重开）"})

        # ===== 交付收口（与旧导出同口径：缓存 + 留痕 + GC）=====
        ok = bool(report.get("checks", {}).get("compatibility", {}).get("zip_intact"))
        if ok:
            try:
                await _persist_export_cache_row(
                    db, scheme=scheme, scheme_id=scheme_id,
                    config_hash=config_hash, content_fingerprint=content_hash,
                    cache_key=f"{scheme_id}_{config_hash[:8]}",
                    out_path=final_path, what="export_cache 写入(两轮次 DOCX)")
            except Exception as e:  # noqa: BLE001 —— 缓存失败不阻断交付
                logger.warning("两轮次导出缓存写入失败（不影响交付）: %s", e)
        await _finalize_export_delivery(
            db, scheme_id=scheme_id, kind="docx", round_no=round_no,
            export_filename=export_filename, gc_protect={final_path})

        # 成功交付：先清理初稿中间产物，再发终态事件
        # （否则订阅方可能在 finally 之前就收到 terminal，读到残留初稿）
        if draft_path is not None and delivered is not None \
                and draft_path.exists() and draft_path != delivered:
            try:
                draft_path.unlink(missing_ok=True)
            except OSError:
                pass

        summary = (report.get("summary") or {})
        msg = (f"导出完成：检出 {summary.get('detected', 0)} 项，"
               f"已修复 {summary.get('fixed', 0)} 项，"
               f"跳过 {summary.get('skipped', 0)} 项，"
               f"需人工 {summary.get('manual', summary.get('unresolved', 0))} 项")
        await _tr.update_progress(task_id, 100.0, msg)
        st["report"] = report
        st["final_path"] = str(delivered)
        st["filename"] = export_filename
        st["mode"] = mode
        await _persist_checkpoint(task_id, report, str(delivered), export_filename, mode)
        await _emit(task_id, {"event": "terminal", "terminal": True,
                              "status": "completed", "progress": 100.0,
                              "message": msg, "report": report,
                              "download": f"/api/v1/schemes/{scheme_id}"
                                          f"/export/flow/{task_id}/download"})
        await _tr.finish_task(task_id, "degraded" if mode != "fixed" else "completed",
                              msg)
    except ExportCancelled:
        logger.info("两轮次导出已取消（scheme=%s task=%s）", scheme_id[:8], task_id)
        st["mode"] = "cancelled"
        await _emit(task_id, {"event": "terminal", "terminal": True,
                              "status": "stopped", "message": "导出已被用户停止"})
        await _tr.finish_task(task_id, "stopped", "用户停止了两轮次导出")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        # ===== 回滚契约：第二轮失败 → 交付初稿（绝不交付半修复产物）=====
        logger.error("两轮次导出失败，回滚初稿（scheme=%s task=%s）: %s",
                     scheme_id[:8], task_id, e, exc_info=True)
        fallback = None
        if draft_path and draft_path.exists():
            try:
                fallback = _rollback_to_draft(draft_path, scheme_id)
            except Exception as re_err:  # noqa: BLE001
                logger.error("初稿回滚亦失败（task=%s）: %s", task_id, re_err)
        if fallback:
            mode = "fallback_draft"
            report = {"mode": mode,
                      "summary": {"detected": 0, "fixed": 0, "skipped": 0,
                                  "manual": 0, "total_findings": 0,
                                  "unresolved": 0,
                                  "unresolved_by_code": [], "by_category": {}},
                      "findings": [], "actions": [], "unresolved": [],
                      "checks": {}, "round1_stats": {},
                      "error": str(e)[:500]}
            st["report"] = report
            st["final_path"] = str(fallback)
            st["filename"] = draft_path.name if draft_path else "export.docx"
            st["mode"] = mode
            await _persist_checkpoint(task_id, report, str(fallback),
                                      st["filename"], mode)
            msg = "修复失败，已回滚交付初稿"
            await _emit(task_id, {"event": "terminal", "terminal": True,
                                  "status": "degraded", "progress": 100.0,
                                  "message": msg, "report": report,
                                  "download": f"/api/v1/schemes/{scheme_id}"
                                              f"/export/flow/{task_id}/download"})
            await _tr.finish_task(task_id, "degraded", msg)
        else:
            st["mode"] = "failed"
            await _emit(task_id, {"event": "terminal", "terminal": True,
                                  "status": "failed",
                                  "message": f"导出失败：{str(e)[:200]}"})
            await _tr.finish_task(task_id, "failed", f"导出失败：{str(e)[:200]}")
    finally:
        watcher.cancel()
        if draft_path is not None and delivered is not None \
                and draft_path.exists() and draft_path != delivered:
            # 初稿已成功转为终稿 → 清理中间产物
            try:
                draft_path.unlink(missing_ok=True)
            except OSError:
                pass
        if manifest_path is not None:
            try:
                manifest_path.unlink(missing_ok=True)
            except OSError:
                pass


def _expected_outline_from_prep(prep: dict) -> list[int] | None:
    """从取数段结果提取目录树层级序列（C1 与目录树一致性比对的基准）。"""
    try:
        roots = prep.get("roots") or []
        children = prep.get("children_map") or {}
        levels: list[int] = []

        def walk(nodes):
            for n in nodes:
                lvl = int(n.get("level") or 1)
                levels.append(lvl)
                walk(children.get(n.get("id") or []) or [])
        walk(roots)
        return levels or None
    except Exception:  # noqa: BLE001 —— 提取失败 = 不比对（只做文档内连续性检查）
        return None


def _rollback_to_draft(draft_path: Path, scheme_id: str) -> Path | None:
    """初稿原子改名交付（同目录唯一名）。"""
    target = draft_path.with_name(f"{scheme_id[:8]}_rollback_{uuid.uuid4().hex[:8]}.docx")
    os.replace(draft_path, target)
    return target


async def _persist_checkpoint(task_id: str, report: dict, final_path: str,
                              filename: str, mode: str) -> None:
    """修复报告 + 交付物路径写入 task_registry.checkpoint_json（断线重挂/重启可查）。"""
    try:
        conn = await get_conn()
        cur = await conn.execute(
            "UPDATE task_registry SET checkpoint_json=? WHERE id=?",
            (json.dumps({"report": report, "final_path": final_path,
                         "filename": filename, "mode": mode},
                        ensure_ascii=False)[:1_000_000], task_id))
        safe_rowcount(cur, what="两轮次导出检查点写入")
        await conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("检查点写入失败（task=%s）: %s", task_id, e)


# ---------------------------------------------------------------------------
# 后台任务保活：asyncio 事件循环对任务仅持弱引用，游离 create_task 可能被 GC
# 提前取消；且未捕获异常只会落 "Task exception was never retrieved" 噪音。
# 这里用模块级强引用登记，done 回调统一留痕并清理，避免长期累积。
# ---------------------------------------------------------------------------
_FLOW_TASKS: set[asyncio.Task] = set()


def _track_flow_task(task: asyncio.Task, task_id: str, scheme_id: str) -> None:
    """登记两轮次导出后台任务：强引用保活 + 异常留痕 + 完成后清理。"""
    _FLOW_TASKS.add(task)

    def _on_done(_task: asyncio.Task) -> None:
        _FLOW_TASKS.discard(_task)
        if _task.cancelled():
            logger.warning("两轮次导出任务被取消（scheme=%s task=%s）",
                           scheme_id[:8], task_id)
            return
        _exc = _task.exception()
        if _exc is not None:
            logger.error("两轮次导出任务未捕获异常（scheme=%s task=%s）: %r",
                         scheme_id[:8], task_id, _exc, exc_info=_exc)

    task.add_done_callback(_on_done)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.post("/{scheme_id}/export/docx/v2")
async def export_docx_v2(scheme_id: str, body: dict | None = None):
    """启动两轮次导出任务（第一轮初稿 + 第二轮检查修复）。202 返回 task_id。"""
    from fastapi.responses import JSONResponse
    body = body or {}
    # 复用 task_registry：同 scheme 同类型僵尸任务自动替换 + /sse/task 控制链路可用
    task_id = await _tr.register_task(TASK_TYPE, scheme_id=scheme_id)
    _state(task_id)
    _flow_task = asyncio.create_task(_run_flow(task_id, scheme_id, body))
    _track_flow_task(_flow_task, task_id, scheme_id)
    logger.info("两轮次导出任务已启动（scheme=%s task=%s）", scheme_id[:8], task_id)
    return JSONResponse(status_code=202, content={
        "ok": True, "task_id": task_id,
        "events": f"/api/v1/schemes/{scheme_id}/export/flow/{task_id}/events",
        "download": f"/api/v1/schemes/{scheme_id}/export/flow/{task_id}/download",
    })


@router.get("/{scheme_id}/export/flow/{task_id}/events")
async def export_flow_events(scheme_id: str, task_id: str):
    """SSE：两段进度 + 修复项实时流；断线重挂接回放全部缓冲事件。"""
    st = _state(task_id)

    async def _gen():
        q: asyncio.Queue = asyncio.Queue()
        async with _FLOW_LOCK:
            for evt in list(st["buffer"]):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                if evt.get("terminal"):
                    return
            st["waiters"].add(q)
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"  # SSE 注释心跳（前端不解析）
                    continue
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                if evt.get("terminal"):
                    return
        finally:
            st["waiters"].discard(q)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/{scheme_id}/export/flow/{task_id}/download")
async def export_flow_download(scheme_id: str, task_id: str):
    """下载终稿（修复成功）或回滚初稿（mode=fallback_draft）。"""
    st = _state(task_id)
    final_path = st.get("final_path")
    filename = st.get("filename") or "export.docx"
    if not final_path:
        # 进程重启后内存态丢失 → 回读 task_registry.checkpoint_json
        try:
            conn = await get_conn()
            cur = await conn.execute(
                "SELECT checkpoint_json, status FROM task_registry WHERE id=?",
                (task_id,))
            row = await cur.fetchone()
            if row and row[0]:
                ck = json.loads(row[0])
                final_path = ck.get("final_path")
                filename = ck.get("filename") or filename
        except Exception:  # noqa: BLE001
            row = None
    if not final_path or not os.path.exists(final_path):
        raise HTTPException(404, "导出产物不存在或尚未完成")
    headers = dict(_export_name_headers(filename, 0))
    return FileResponse(final_path, filename=filename,
                        headers=headers, media_type=_DOCX_MIME)


@router.post("/{scheme_id}/export/flow/{task_id}/cancel")
async def export_flow_cancel(scheme_id: str, task_id: str):
    """真停止：置位 task_registry 停止事件 → 构建线程下一检查点退出。"""
    ok = _tr.request_control(task_id, "stop")
    if not ok:
        await _tr.set_task_status(task_id, "stopped")
    return {"ok": True, "in_progress": bool(ok)}
