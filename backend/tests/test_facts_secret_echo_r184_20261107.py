"""R184（2026-11-07）全局事实模块 · 异常文本回显脱敏回归。

父线程转来他模块定位的泄露面：`global_facts.py` 把异常原文放进
`HTTPException.detail`，厂商错误体中的 `Authorization: Bearer sk-…` 会随响应
回给前端。本文件逐点证实/证伪：

1. ``POST /api/v1/global-facts/adjust``（AI 调整）：``except Exception as e``
   旧实现 `raise HTTPException(502, f"事实调整失败：{e}")`。`e` 来自 AI 调用链
   （含厂商 HTTP 错误体），是**真实可达**的泄露面 → 修复后必须掩码。
2. 写保护 409（``FactsWriteConflict``）：消息由 `holder + project_id` 本地拼接，
   生产调用方只传 ``METHOD /path`` / 固定 worker 名，**当前不存在**带凭据的
   可达路径；但按「异常文本一律脱敏」统一出口加固，防止未来调用方传入带凭据
   文本。本文件用带 `sk-` 的 holder 证明加固生效。
"""

import json
from types import SimpleNamespace

import app.routers.global_facts as gf
import pytest
from app.models import AdjustRequest
from app.services.facts_write_guard import FactsWriteConflict
from fastapi import HTTPException

PID = "proj-r184"
SID = "scheme-r184"
SECRET = "sk-live-abc1234567890"


async def _seed_scope_and_fact(db):
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PID, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (SID, PID, "s"))
    await db.execute(
        "INSERT INTO global_facts(id, project_id, scheme_id, group_id, group_title, title,"
        " content, category, is_simulated, is_resolved, has_conflict, is_stale, fact_key)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "f-r184",
            PID,
            SID,
            "g1",
            "人员",
            "项目经理",
            "- **项目经理**: 张三",
            "personnel",
            0,
            1,
            0,
            0,
            "project_manager",
        ),
    )
    await db.commit()


async def test_adjust_vendor_error_masks_secret(db_conn, monkeypatch):
    """真实泄露面：厂商 401 错误体经 502 detail 回显时必须掩码。"""
    await _seed_scope_and_fact(db_conn)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError(
            'HTTP 401: {"error":{"message":"Incorrect API key provided: '
            f'Authorization: Bearer {SECRET}"}}'
        )

    monkeypatch.setattr(gf, "collect_json_response", _boom)

    with pytest.raises(HTTPException) as ei:
        await gf.adjust_facts(
            AdjustRequest(instruction="把所有年份改成 2026", scheme_id=SID, apply=False),
            db=db_conn,
        )
    assert ei.value.status_code == 502
    detail_text = json.dumps(ei.value.detail, ensure_ascii=False)
    assert SECRET not in detail_text, "厂商错误体中的密钥被 502 detail 原样回显"
    assert "sk-****" in detail_text or "Bearer ****" in detail_text, "掩码后仍应保留可诊断的错误上下文"


class _FakeRequest:
    method = "POST"
    url = SimpleNamespace(path="/api/v1/global-facts")


async def test_write_guard_conflict_message_is_masked(db_conn, monkeypatch):
    """写保护 409：即使 holder 意外携带凭据，也不得原样回显。"""

    async def _fake_scope(_request, _db):
        return PID

    async def _fake_acquire(_db, project_id, holder=None):
        raise FactsWriteConflict(project_id, f"worker Authorization: Bearer {SECRET}")

    monkeypatch.setattr(gf, "_facts_write_scope_project_id", _fake_scope)
    monkeypatch.setattr(gf, "acquire_project_facts_write", _fake_acquire)

    agen = gf._facts_write_guard_dependency(_FakeRequest(), db_conn)
    with pytest.raises(HTTPException) as ei:
        await anext(agen)
    assert ei.value.status_code == 409
    detail_text = json.dumps(ei.value.detail, ensure_ascii=False)
    assert SECRET not in detail_text
    assert "Bearer ****" in detail_text or "sk-****" in detail_text


def test_no_unredacted_exception_echo_left_in_router_source():
    """源码守卫：路由内不得再出现 `{e}` / `str(e)` 直出 HTTPException.detail 的写法。"""
    from pathlib import Path

    src = Path(gf.__file__).read_text(encoding="utf-8")
    forbidden = [
        'HTTPException(502, f"事实调整失败：{e}")',
        'detail={"code": "facts_write_conflict", "message": str(e)}',
    ]
    for item in forbidden:
        assert item not in src, f"发现未脱敏的异常回显：{item}"


async def test_gate_load_failure_log_is_masked(db_conn, monkeypatch, caplog):
    """R186 自查补漏：闸门读失败（fail-closed）的日志也不得原样写异常文本。

    该分支当前只会收到 sqlite 错误 / 静态 404，**尚无带凭据的可达路径**；
    但它是本模块最后一个未过脱敏出口的异常日志，按 R184 的统一标准补齐。
    响应体本身已是静态 503 文案，修复前也只有日志泄露。
    """
    import logging

    async def _boom(*_args, **_kwargs):
        raise RuntimeError(f"db read failed: Authorization: Bearer {SECRET}")

    monkeypatch.setattr(gf, "_load_fact_rows", _boom)
    with caplog.at_level(logging.WARNING, logger="global_facts"):
        with pytest.raises(HTTPException) as ei:
            await gf._load_injectable_peers_for_gate(db_conn, SID, PID)
    assert ei.value.status_code == 503
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in log_text, "闸门读失败日志泄露了异常原文中的密钥"
    assert "sk-****" in log_text or "Bearer ****" in log_text, "掩码后仍应保留可诊断上下文"
