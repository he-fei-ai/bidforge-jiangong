"""T3/T4 守护锁（2026-10-04）：

T3 · 历史备份文件守护
    一次性清理了 20 个 ``*.bak-*`` 备份文件。本守护静态扫描仓库根，
    断言此类文件数为 0，防止回归。

T4 · Deprecated 端点响应头契约
    compliance.py 中三个 ``@deprecated`` 端点已注入
    Deprecation / Sunset / Link 三个 RFC 8594 标准响应头。
    本守护用 ASGITransport 打真实端点，断言：
      - 三个 header 都存在且格式正确
      - 响应体结构未变（向后兼容）
      - ``Link`` 指向明确的替代端点路径
"""
from __future__ import annotations

import fnmatch
import os
import re
import uuid
from pathlib import Path

import app.db as _appdb
import httpx
import pytest
from app.db import close_db, get_conn, init_db
from app.main import app

REPO_ROOT = str(Path(__file__).resolve().parents[2])

_BAK_GLOBS = ("*.bak-*", "*.bak", "*.bak~", "*~")

#: ⚠️ R85（2026-10-16）：本护栏此前 `os.walk(REPO_ROOT)` 遍历**整个仓库**，
#: 而 `backend/data/projects/<pid>/documents/**` 是四层存储的运行时产物目录，
#: 实测已膨胀到 **2.9 万个目录**（单次 os.walk > 120s）⇒ `test_no_bak_files_in_repo`
#: 无限期挂起，**整条后端全量 pytest 从此跑不完**（R84 的 32 分钟全量基线不再
#: 可复现）。属 AGENTS §4.17.3 记过的同族缺陷：「静态护栏本身也是性能敏感代码，
#: 写递归全仓扫描前先量一下耗时」。
#:
#: 排除项的**判据**是「该目录只可能装运行时/生成物，永远不会是源码」：
#:   · `backend/data`  —— 解析四层存储 + SQLite + 导出产物（运行时数据）
#:   · `logs`           —— 日志轮转输出
#:   · `artifacts`      —— 历轮探针/报告归档（AGENTS §3.1.9 的临时产物区）
#:   · `.trash`         —— 回收站
#:   · `.ruff_cache` / `.pytest_cache` —— 工具缓存
#: **刻意不排除** `backend/app` / `backend/tests` / `frontend/src` / `docs` /
#: `tools` / `chart-gap-filler` / 根目录文件 —— 源码与文档仍全量扫描，
#: 护栏的原始意图（人工作业的 swap 备份只可能出现在源码目录）一字未弱化。
_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", "coverage", ".pytest_cache",
    # ↓ R85：运行时/生成物目录（只可能装数据，不可能装源码备份）
    "data", "logs", "artifacts", ".trash", ".ruff_cache",
}

#: R85 预算锁：全量扫描的**目录数上限**。超限即说明又有人往被排除树之外
#: 塞了巨型目录，护栏会先变红而不是先挂起 —— 「挂起」是最坏的失败形态
#: （既不报错也不出结论，只能靠外部 kill）。
_MAX_WALK_DIRS = 20000


def _is_bak(name: str) -> bool:
    for pat in _BAK_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _walk_all_files():
    seen = 0
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        seen += 1
        if seen > _MAX_WALK_DIRS:
            raise AssertionError(
                f"护栏扫描目录数超过预算 {_MAX_WALK_DIRS}"
                f"（已扫到 {os.path.relpath(root, REPO_ROOT)}）。"
                "请把新的巨型运行时/生成物目录加入 _EXCLUDE_DIRS，"
                "而不是放任护栏挂起。")
        for f in files:
            yield os.path.join(root, f)


# ===========================================================================
# T3 · .bak 备份文件守护
# ===========================================================================
def test_no_bak_files_in_repo():
    """仓库内不得再出现历史备份文件（.bak-* / .bak / *~）。"""
    offenders = []
    for f in _walk_all_files():
        if _is_bak(os.path.basename(f)):
            try:
                rel = os.path.relpath(f, REPO_ROOT).replace("\\", "/")
            except ValueError:
                continue
            offenders.append(rel)
    assert not offenders, (
        f"发现 {len(offenders)} 个历史备份文件，必须清理后再提交：\n"
        + "\n".join(offenders[:20]))


# ===========================================================================
# T4 · Deprecated 端点响应头契约
# ===========================================================================
def _expect_deprecation_headers(headers) -> None:
    """断言响应头包含 RFC 8594 三件套。"""
    assert headers.get("Deprecation") == "true", (
        f"Deprecation 头必须为 'true'，实际 {headers.get('Deprecation')!r}")
    sunset = headers.get("Sunset", "")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", sunset), (
        f"Sunset 头必须是 RFC 3339 时间戳，实际 {sunset!r}")
    link = headers.get("Link", "")
    assert re.match(r"^<.+>;\s*rel=\"deprecation\"$", link), (
        f"Link 头必须是 <...>; rel=\"deprecation\" 形态，实际 {link!r}")


@pytest.fixture
async def dep_db(tmp_path):
    original = _appdb.DB_PATH
    _appdb.DB_PATH = tmp_path / "dep-20261004.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.commit()
    yield sid
    await close_db()
    _appdb.DB_PATH = original


async def test_dimensions_endpoint_has_deprecation_headers(dep_db):
    """GET /api/v1/compliance/dimensions — 响应体不变，新增 3 个 header。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            timeout=30.0) as c:
        resp = await c.get("/api/v1/compliance/dimensions")
    assert resp.status_code == 200, resp.text[:300]
    _expect_deprecation_headers(resp.headers)
    body = resp.json()
    assert "items" in body and "rule_version" in body, (
        f"响应体向后兼容要求不变，实际键 {list(body)}")
    assert resp.headers["Link"].startswith(
        "</api/v1/compliance/overview/{scheme_id}>;")


async def test_consistency_audit_history_has_deprecation_headers(dep_db):
    """GET /api/v1/compliance/consistency-audit/{scheme_id}/history — 契约锁。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            timeout=30.0) as c:
        resp = await c.get(
            f"/api/v1/compliance/consistency-audit/{dep_db}/history",
            params={"limit": 5})
    assert resp.status_code == 200, resp.text[:300]
    _expect_deprecation_headers(resp.headers)
    body = resp.json()
    assert body == {"items": []}, (
        f"空历史响应体应恒为 {{'items': []}}，实际 {body}")
    assert f"/api/v1/compliance/runs/{dep_db}" in resp.headers["Link"]


async def test_preflight_endpoint_has_deprecation_headers(dep_db):
    """POST /api/v1/compliance/preflight/{scheme_id} — 3 个 header + 契约锁。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            timeout=30.0) as c:
        resp = await c.post(f"/api/v1/compliance/preflight/{dep_db}")
    assert resp.status_code == 200, resp.text[:300]
    _expect_deprecation_headers(resp.headers)
    body = resp.json()
    assert isinstance(body, dict) and body, (
        f"预检响应必须是对象，实际 {type(body).__name__}")
    assert f"/api/v1/compliance/overview/{dep_db}" in resp.headers["Link"]
