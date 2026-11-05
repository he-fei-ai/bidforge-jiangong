# -*- coding: utf-8 -*-
"""R92（2026-10-17 · 正文写点同步补链）护栏。

背景（根因 B）：事实冲突定点修复（sse）/ 全文一致性修复（repair_agent）/ 审核预检
自动修复（review_autofix）/ 回滚与快照恢复（repair_record、consistency_repair）/ AI 压缩
（sections.shrink_section）都在**改写已落库正文**，若不按最终正文全量重登记
chart_predictions，则前端 PNG 键（旧登记码）与后端内联码（新正文）漂移 → 导出
「[图 X-Y … — 渲染失败]」红字。

本护栏锁定三件事：
1. 唯一出口原语 `resync_inline_charts` / `resync_inline_chart_rows` 的行为契约
   （围栏内改写→行随正文更新 / 幂等 no-op / 非法块删+清登 / 委派等价 / 纯计算不碰 DB）；
2. 五个正文裸写点 + 回滚链的**接线**（静态源码锁，摘除接线即恒红）；
3. 两条边界判据：numbering 只重写子标题行不动围栏 → **刻意不接线**；services 层
   不得 import routers → repair_agent/repair_record 用注入回调（分层合规锁）。
"""

import asyncio
import json
from pathlib import Path

from app.routers._chart_pipeline import (
    register_inline_charts,
    resync_inline_chart_rows,
    resync_inline_charts,
)

# ---------- 工具 ----------

_APP = Path(__file__).resolve().parents[1] / "app"


def _src(rel: str) -> str:
    return (_APP / rel).read_text(encoding="utf-8")


class FakeDb:
    """最小异步 db mock：记录 execute/commit 调用（与真实 aiosqlite 语义一致）。"""

    def __init__(self):
        self.rows = []
        self.commits = 0

    async def execute(self, sql, params=None):
        self.rows.append((sql, tuple(params or ())))

        class _FakeCur:
            rowcount = 1
            lastrowid = 1

        return _FakeCur()

    async def commit(self):
        self.commits += 1

    def inserts(self):
        return [r for r in self.rows if r[0].startswith("INSERT INTO chart_predictions")]

    def deletes(self):
        return [r for r in self.rows if r[0].startswith("DELETE FROM chart_predictions")]


def run(coro):
    return asyncio.run(coro)


VALID_FLOWCHART = "flowchart TD\n    A --> B\n    B --> C"


# ---------- 组 A：resync_inline_charts（DB 版唯一出口）行为契约 ----------


def test_resync_fence_rewrite_rows_follow_content():
    """核心不变式：围栏内文字改写后重登记 → INSERT 的 mermaid_code 逐字等于最终正文内联码。"""
    db = FakeDb()
    # 事实修复前：正文与登记都是「提前2天」
    before = "前文\n```mermaid\nflowchart TD\n    A -->|提前2天| B --> C\n```\n后文"
    n0, _ = run(resync_inline_charts(db, "sch", "sec-a", before))
    assert n0 == 1
    code0 = json.loads(db.inserts()[-1][1][7])["mermaid_code"]
    assert "提前2天" in code0
    # 事实修复后：正文改成「提前14天」，须按新正文重登记
    db2 = FakeDb()
    after = "前文\n```mermaid\nflowchart TD\n    A -->|提前14天| B --> C\n```\n后文"
    n1, new_content = run(resync_inline_charts(db2, "sch", "sec-a", after))
    assert n1 == 1
    code1 = json.loads(db2.inserts()[-1][1][7])["mermaid_code"]
    assert "提前14天" in code1 and "提前2天" not in code1
    # 两侧逐字一致：登记码必须出现在最终正文围栏内
    assert code1 in new_content
    # 全量同步：先清后登
    dels = db2.deletes()
    assert dels and dels[0][0] == "DELETE FROM chart_predictions WHERE section_id=?"
    assert dels[0][1] == ("sec-a",)
    assert db2.commits == 0  # 不 commit，由调用方同事务提交


def test_resync_is_idempotent():
    """同一正文重复重登记 → (登记数, 正文) 与 INSERT 载荷完全一致（no-op 稳定）。"""
    content = "前文\n```mermaid\n" + VALID_FLOWCHART + "\n```\n后文"
    db1 = FakeDb()
    n1, c1 = run(resync_inline_charts(db1, "sch", "sec-idem", content))
    db2 = FakeDb()
    n2, c2 = run(resync_inline_charts(db2, "sch", "sec-idem", content))
    assert (n1, c1) == (n2, c2) == (1, content)
    assert json.loads(db1.inserts()[-1][1][7]) == json.loads(db2.inserts()[-1][1][7])


def test_resync_invalid_block_drops_and_clears():
    """非法块（修复失败）→ 从正文删块 + 清理历史登记，绝不残留僵尸图（宁缺勿滥）。"""
    db = FakeDb()
    content = "前文\n```mermaid\ngraph TD\nA-[坏\n```\n后文"
    n, new_content = run(resync_inline_charts(db, "sch", "sec-bad", content))
    assert n == 0
    assert "```mermaid" not in new_content
    assert "前文" in new_content and "后文" in new_content
    assert db.inserts() == []
    assert db.deletes() and db.deletes()[0][1] == ("sec-bad",)


def test_register_delegates_to_resync():
    """既有入口 register_inline_charts 委派 resync_inline_charts：同输入产物逐字一致。"""
    content = "前文\n```mermaid\n" + VALID_FLOWCHART + "\n```\n后文"
    dba = FakeDb()
    ra = run(register_inline_charts(dba, "sch", "sec-d", content))
    dbb = FakeDb()
    rb = run(resync_inline_charts(dbb, "sch", "sec-d", content))
    assert ra == rb
    assert [r[0] for r in dba.rows] == [r[0] for r in dbb.rows]


# ---------- 组 B：resync_inline_chart_rows（锁外纯计算版）契约 ----------


def test_resync_rows_is_pure_no_db():
    """纯计算版不访问 DB：FakeDb 零 execute 调用，产物是 (正文, rows)。"""
    db = FakeDb()
    content = "前文\n```mermaid\n" + VALID_FLOWCHART + "\n```\n后文"
    # 纯函数不接收 db —— 若误加 DB 访问会在无 db 参数时 TypeError，此处仅按签名调用
    new_content, rows = resync_inline_chart_rows("sch", "sec-pure", content, enforce_limits=False)
    assert new_content == content
    assert isinstance(rows, list) and len(rows) == 1
    assert db.rows == []  # 未触碰任何 DB


def test_resync_rows_matches_db_version_registration():
    """纯计算版产出的 rows 与 DB 版登记条数一致（同一管线，仅拆构建/落库两步）。"""
    content = "前文\n```mermaid\n" + VALID_FLOWCHART + "\n```\n后文"
    _, rows = resync_inline_chart_rows("sch", "sec-x", content, enforce_limits=False)
    db = FakeDb()
    n, _ = run(resync_inline_charts(db, "sch", "sec-x", content))
    assert n == len(rows) == 1


# ---------- 组 C：五个正文裸写点 + 回滚链 接线静态锁 ----------


def test_sse_autofix_rewires_chart_rows():
    src = _src("routers/sse_handlers.py")
    assert "resync_inline_chart_rows" in src
    # 事实修复 applied 后必须按修复后正文重建配对（顺序：applied 分支 → resync）
    applied = src.index('if _fa_res.get("applied"):')
    resync = src.index("resync_inline_chart_rows(")
    assert applied < resync


def test_repair_agent_wires_resync_callback():
    src = _src("services/repair_agent.py")
    assert "resync_fn=None" in src
    assert "await resync_fn(" in src
    # 校正后正文落库（_persist_content 来自 resync 结果），而非裸用 after
    assert "_persist_content" in src


def test_review_autofix_persist_wires_resync():
    src = _src("routers/review_autofix.py")
    assert "from app.routers._chart_pipeline import resync_inline_charts" in src
    assert "await resync_inline_charts(" in src
    # rollback 端点用注入回调
    assert "resync_fn=_resync" in src


def test_repair_record_rollback_wires_resync():
    src = _src("services/repair_record.py")
    assert "resync_fn=None" in src
    assert "await resync_fn(" in src


def test_consistency_repair_wires_resync_factory():
    src = _src("routers/consistency_repair.py")
    assert "def _make_chart_resync(" in src
    assert "resync_inline_charts" in src
    # repair() 传回调、confirm/rollback 走恢复链
    assert "resync_fn=_resync_charts" in src
    assert "resync_fn=_make_chart_resync(scheme_id)" in src


def test_sections_shrink_wires_resync():
    src = _src("routers/sections.py")
    assert "resync_inline_charts," in src
    # shrink 端点 UPDATE 前按压缩后正文重登记
    shrink = src.index("async def shrink_section")
    assert src.index("await resync_inline_charts(", shrink) > shrink


# ---------- 组 D：边界判据（刻意不接线 + 分层合规） ----------


def test_numbering_does_not_wire_resync():
    """编号规范化只重写子标题行、不动代码围栏 → 无需重登记（判据锁，防误接）。"""
    src = _src("services/numbering.py")
    assert "resync_inline_charts" not in src
    assert "register_inline_charts" not in src


def test_services_never_import_routers():
    """分层约束：services 层用注入回调（resync_fn），绝不 import routers。"""
    for rel in ("services/repair_agent.py", "services/repair_record.py"):
        src = _src(rel)
        assert "from app.routers" not in src
        assert "import app.routers" not in src
