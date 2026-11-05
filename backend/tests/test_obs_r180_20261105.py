# -*- coding: utf-8 -*-
"""R180 · 可观测性收口（2026-11-05）。

覆盖本次矩阵收口清单中「没有数据就没法判断」的 4 项：
  1. 一致性校核：每次审计都写覆盖摘要（counts_json），历史行零迁移可读；
  2. 提示词：版本变更/渲染次数的「今日」计数可查询，且不改既有返回键；
  4. 图表渲染失败：结构化失败明细（图型/章节/原因/是否占号）可从日志聚合；
  5. 任务取消：终态原因归一为稳定枚举（客户端离开/用户手动/超时/进程重启）。
"""

import json
from pathlib import Path

import pytest
from app.routers import compliance
from app.routers.prompts import get_prompt_metrics
from app.services.ai.task_registry import _infer_cancel_reason

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORT_SRC = (REPO_ROOT / "backend" / "app" / "routers" / "export.py").read_text(encoding="utf-8")


# ===========================================================================
# 1 · 一致性校核：覆盖摘要落库 + 历史行兼容
# ===========================================================================


def test_consistency_audit_schema_has_counts_json():
    import inspect

    import app.schema_sql as ss

    src = inspect.getsource(ss)
    assert "counts_json TEXT DEFAULT '{}'" in src


@pytest.mark.asyncio
async def test_consistency_audit_counts_roundtrip_and_legacy_compat(db_conn):
    # 历史行（counts_json = ''）：读侧必须 fail-open 成 {}
    await db_conn.execute(
        "INSERT INTO consistency_audit (id, project_id, scheme_id, score, issues, counts_json) "
        "VALUES (?,?,?,?,?,?)",
        ("old", "p1", "s1", 0.9, json.dumps([]), ""),
    )
    counts = {"sections": 21, "batches": 3, "checked_chars": 18000, "total_chars": 21000, "issues": 0}
    await db_conn.execute(
        "INSERT INTO consistency_audit (id, project_id, scheme_id, score, issues, counts_json) "
        "VALUES (?,?,?,?,?,?)",
        ("new", "p1", "s1", 0.8, json.dumps([{"type": "x"}]), json.dumps(counts)),
    )
    await db_conn.commit()

    latest = await compliance.latest_consistency_audit("s1", db=db_conn)
    assert latest["exists"] is True
    assert latest["counts"] == counts, "最近一次审计必须能从 DB 回答覆盖节数/字数/问题数"
    assert latest["issues"] == [{"type": "x"}]

    hist = await compliance.consistency_audit_history("s1", db=db_conn, limit=10)
    by_id = {r["id"]: r for r in hist["items"]}
    assert by_id["new"]["counts"] == counts
    assert by_id["old"]["counts"] == {}, "历史行（空串）必须解析为空对象而不是报错"


def test_consistency_audit_insert_writes_counts():
    import inspect

    src = inspect.getsource(compliance)
    assert "counts_json) VALUES" in src
    assert '"checked_chars": _sent_chars' in src
    assert '"total_chars": len(content)' in src


# ===========================================================================
# 2 · 提示词：今日计数可查询（且旧键不变）
# ===========================================================================


@pytest.mark.asyncio
async def test_prompt_metrics_today_counts(db_conn):
    await db_conn.execute(
        "INSERT INTO prompt_audit_logs (id, prompt_key, action) VALUES (?,?,?)",
        ("a1", "content.draft", "override"),
    )
    await db_conn.execute(
        "INSERT INTO ai_audit_logs (id, provider_name, action, success) VALUES (?,?,?,?)",
        ("c1", "custom", "content_draft", 1),
    )
    await db_conn.commit()

    snap = await get_prompt_metrics(db=db_conn)
    for _k in ("render_total", "render_errors", "token_budget_truncated", "repair_triggered", "registered_keys"):
        assert _k in snap, f"既有返回键 {_k} 不得因新增 today 而消失"
    today = snap["today"]
    assert today["version_changes_today"] == 1
    assert today["render_calls_today"] == 1
    assert "render_basis" in today, "近似口径必须对使用者显式标注"


# ===========================================================================
# 4 · 图表渲染失败：结构化明细（图型/章节/原因/是否占号）
# ===========================================================================


def test_chart_render_failure_is_structured():
    assert "def _note_render_failure(" in EXPORT_SRC
    assert '"failed_details"' in EXPORT_SRC
    for _k in ("chart_type", "section_id", "section_title", "code_hash", "reason", "occupies_number"):
        assert f'"{_k}"' in EXPORT_SRC, f"失败明细缺少字段 {_k}"
    assert "_note_render_failure(ct, code, \"render_returned_none\")" in EXPORT_SRC
    assert 'f"exception:{type(e).__name__}"' in EXPORT_SRC
    # 可聚合的结构化日志（图型+章节+原因+是否占号）
    assert "chart_render_failure chart_type=%s section_id=%s" in EXPORT_SRC


def test_render_stats_header_is_latin1_safe():
    """失败明细含中文 section_title → 响应头必须 ASCII 安全。

    Starlette ``FileResponse.init_headers`` 用 latin-1 编码 header 值；
    ``ensure_ascii=False`` 会把中文原样写进 ``X-Chart-Render-Stats``，
    抛 ``UnicodeEncodeError`` 直接打断 DOCX/PDF 交付（R180 首版实测 10 项导出
    测试同时失败）。这里同时锁源码口径与真实可编码性：
    """
    import re

    # 源码口径：每个 X-Chart-Render-Stats 值（截到下一个响应头键为止）都必须
    # ensure_ascii=True，不得混入 ensure_ascii=False。
    starts = [m.start() for m in re.finditer(r'"X-Chart-Render-Stats":', EXPORT_SRC)]
    assert len(starts) >= 3, "DOCX 缓存/非缓存与 PDF 三处都应回传渲染统计"
    for i, st in enumerate(starts):
        nxt = re.search(r'"X-[A-Za-z-]+":', EXPORT_SRC[st + 25 :])
        end = st + 25 + nxt.start() if nxt else len(EXPORT_SRC)
        block = EXPORT_SRC[st:end]
        # 缓存命中分支是纯 ASCII 字面量（`{"cached":true}`），其余必须 ensure_ascii=True
        assert "ensure_ascii=True" in block or '\'{"cached":true}\'' in block, (
            f"X-Chart-Render-Stats 第 {i + 1} 处未使用 ASCII 安全序列化"
        )
        assert "ensure_ascii=False" not in block, f"X-Chart-Render-Stats 第 {i + 1} 处写入了非 ASCII 原文"

    # 真实可编码性：同一份含中文明细的统计可被 latin-1 编码且 JSON 回读无损
    stats = {
        "failed": 1,
        "failed_details": [
            {
                "chart_type": "flowchart",
                "section_id": "c1",
                "section_title": "第一章 总体概述",
                "code_hash": "abcd1234",
                "reason": "render_returned_none",
                "occupies_number": False,
            }
        ],
    }
    header = json.dumps(stats, ensure_ascii=True)
    header.encode("latin-1")  # 不抛 UnicodeEncodeError
    assert json.loads(header)["failed_details"][0]["section_title"] == "第一章 总体概述"


# ===========================================================================
# 5 · 任务取消来源：稳定枚举（不改 DB 结构，仅日志侧）
# ===========================================================================


@pytest.mark.parametrize(
    "status,message,expected",
    [
        ("completed", "", "completed"),
        ("degraded", "7/23 段未成功", "partial_failure"),
        ("failed", "必填解析项未完成", "task_failure"),
        ("stopped", "客户端断开，任务已终止", "client_disconnect"),
        ("stopped", "客户端离开", "client_disconnect"),
        ("stopped", "用户已停止", "user_stop"),
        ("stopped", "任务已取消", "task_cancelled"),
        ("stopped", "服务重启，任务中断", "process_restart"),
        ("stopped", "执行超时", "timeout"),
        ("stopped", "无订阅者", "reaper_no_subscriber"),
        ("stopped", "", "stopped_unknown"),
    ],
)
def test_infer_cancel_reason(status, message, expected):
    assert _infer_cancel_reason(status, message) == expected


def test_terminal_log_contains_reason():
    import inspect

    from app.services.ai import task_registry as tr

    src = inspect.getsource(tr.finish_task)
    assert "_infer_cancel_reason(status, message)" in src
    assert "reason=%s" in src


# ===========================================================================
# 6. 合规批次审计：16 列（含 error_class）而非旧 13 列
# ===========================================================================
async def test_compliance_batch_audit_failure_row_is_classified(db_conn):
    """合规路径写入的失败行必须带 error_class（否则统计归 unknown）。

    旧实现按 13 列 INSERT，``error_class`` 恒为空；且新库已扩到 16 列，
    失败行在 ``/ai/stats`` 的 by_error 中全部落入 ``unknown``。
    """
    await compliance._record_batch_audit(
        db_conn,
        index=1,
        total=3,
        chars=5980,
        duration=1.25,
        success=False,
        note="HTTP 429 Too Many Requests",
    )
    cur = await db_conn.execute(
        "SELECT success, error_class, attempt_count, attempts_json FROM ai_audit_logs"
        " WHERE action='compliance_batch' ORDER BY rowid DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row["success"] == 0
    assert row["error_class"] == "rate_limit", "失败行必须按错误原文归类，不得落 unknown"
    assert row["attempt_count"] == 1
    assert row["attempts_json"] == "[]"


async def test_compliance_batch_audit_success_row_keeps_empty_error_class(db_conn):
    """成功行 error_class 留空（不得把 batch= 摘要误分类成某个错误类别）。"""
    await compliance._record_batch_audit(
        db_conn, index=1, total=1, chars=1000, duration=0.5
    )
    cur = await db_conn.execute(
        "SELECT success, error_class FROM ai_audit_logs"
        " WHERE action='compliance_batch' ORDER BY rowid DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row["success"] == 1
    assert row["error_class"] == ""
