"""R206（2026-11-09）护栏：L-12（WAL 跨进程可见）+ L-14（提示词审计版本号）。

L-12：SQLite WAL 模式下跨进程写入落在 ``-wal`` 文件，**主库文件 mtime 不变**
      ⇒ 旧实现把 mtime 当**前置**门禁 ⇒ 表级水印那第二道闸永远走不到
      ⇒ 外部进程/脚本/迁移工具写入的提示词在服务进程内长期不可见。
      现以表级水印为权威判据，mtime 降级为「无水印基线」时的粗判兜底。

L-14：``prompt_audit_logs.created_at`` 仅秒级精度 ⇒ 同秒多次变更先后不可判定。
      现引入全局单调序号列 ``version``（事务内自增），列表按 version 倒序。
"""
import os
import sqlite3
import time

import pytest
from app.services.ai.prompts import _cache as PC


# ===========================================================================
# L-12 · WAL 跨进程提示词变更可见
# ===========================================================================
@pytest.fixture
def cache_state():
    """快照/恢复 _cache 模块级状态（跨文件共享，必须还原）。"""
    snap = (
        PC._prompt_cache,
        PC._loaded_db_path,
        PC._loaded_db_mtime,
        PC._loaded_db_watermark,
        PC._loaded_db_missing,
        PC._watermark_probe_ts,
    )
    PC._prompt_cache = None
    PC._loaded_db_path = None
    PC._loaded_db_mtime = None
    PC._loaded_db_watermark = None
    PC._loaded_db_missing = False
    PC._watermark_probe_ts = 0.0
    yield
    (
        PC._prompt_cache,
        PC._loaded_db_path,
        PC._loaded_db_mtime,
        PC._loaded_db_watermark,
        PC._loaded_db_missing,
        PC._watermark_probe_ts,
    ) = snap


def _make_wal_db(path):
    """建一个 WAL 模式库并**保持连接打开**（避免关连接触发 checkpoint）。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE prompt_templates (key TEXT PRIMARY KEY, content TEXT,"
        " updated_at TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.execute("INSERT INTO prompt_templates(key, content, updated_at) VALUES ('K1','v1','t0')")
    conn.commit()
    return conn


async def test_wal_cross_process_write_visible(cache_state, tmp_path, monkeypatch):
    """WAL 下另一进程写入 → 缓存必须重载且新内容可见（主库 mtime 不变）。"""
    db = tmp_path / "wal.sqlite"
    keeper = _make_wal_db(db)
    try:
        monkeypatch.setattr(PC, "_current_db_path", lambda: str(db))
        cache = PC._get_prompt_cache()
        assert cache["K1"] == "v1"
        assert PC._loaded_db_watermark is not None, "有水印基线才走权威判据"

        mtime_before = os.stat(db).st_mtime
        time.sleep(0.02)

        # 另一连接（模拟另一进程）写入 —— 只落在 -wal，主库 mtime 不变。
        other = sqlite3.connect(str(db))
        try:
            other.execute("UPDATE prompt_templates SET content='v2', updated_at='t9' WHERE key='K1'")
            other.commit()
        finally:
            other.close()

        assert os.stat(db).st_mtime == mtime_before, "前置条件：WAL 下主库 mtime 未变（旧实现因此看不见）"

        PC._watermark_probe_ts = 0.0  # 放开节流，强制本轮真探测
        cache = PC._get_prompt_cache()
        assert cache["K1"] == "v2", "WAL 下跨进程写入必须对缓存可见（水印为权威判据）"
    finally:
        keeper.close()


async def test_non_wal_other_table_bump_no_reload(cache_state, tmp_path, monkeypatch):
    """回归（R108 语义不变）：他表写入 bump 库 mtime + 水印未变 → **不重载**。"""
    db = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE prompt_templates (key TEXT PRIMARY KEY, content TEXT,"
        " updated_at TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.execute("CREATE TABLE other_table (x)")
    conn.execute("INSERT INTO prompt_templates(key, content, updated_at) VALUES ('K1','v1','t0')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(PC, "_current_db_path", lambda: str(db))
    loads = {"n": 0}
    orig = PC._load_prompt_cache_sync

    def counted():
        loads["n"] += 1
        orig()

    monkeypatch.setattr(PC, "_load_prompt_cache_sync", counted)

    PC._get_prompt_cache()
    assert loads["n"] == 1

    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO other_table VALUES (1)")
    conn.commit()
    conn.close()
    st = os.stat(db)
    os.utime(db, (st.st_atime + 10, st.st_mtime + 10))

    PC._watermark_probe_ts = 0.0
    cache = PC._get_prompt_cache()
    assert loads["n"] == 1, "水印未变时绝不允许全量重载（R108 原意保留）"
    assert cache["K1"] == "v1"


async def test_no_watermark_baseline_falls_back_to_mtime(cache_state, tmp_path, monkeypatch):
    """无水印基线（极旧库/库文件后创建）→ 退回 mtime 粗判（R179 语义不变）。"""
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE prompt_templates (key TEXT PRIMARY KEY, content TEXT,"
        " updated_at TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.execute("INSERT INTO prompt_templates(key, content, updated_at) VALUES ('K1','v1','t0')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(PC, "_current_db_path", lambda: str(db))
    PC._get_prompt_cache()
    PC._loaded_db_watermark = None  # 模拟「水印不可用」（极旧库缺列）
    st = os.stat(db)
    os.utime(db, (st.st_atime + 10, st.st_mtime + 10))

    loads = {"n": 0}
    orig = PC._load_prompt_cache_sync

    def counted():
        loads["n"] += 1
        orig()

    monkeypatch.setattr(PC, "_load_prompt_cache_sync", counted)
    PC._get_prompt_cache()
    assert loads["n"] == 1, "无水印基线时 mtime 变新仍须重载（旧行为逐字保留）"


# ===========================================================================
# L-14 · 提示词审计版本号（全局单调序号）
# ===========================================================================
async def test_audit_version_monotonic(db_conn):
    """连续写入 → version 严格递增（1,2,3...）。"""
    from app.services.audit_service import record_prompt_audit

    for i in range(3):
        await record_prompt_audit(db_conn, "K", "update", f"b{i}", f"a{i}", [], [], None)
    await db_conn.commit()

    cur = await db_conn.execute("SELECT version FROM prompt_audit_logs ORDER BY version")
    versions = [r[0] for r in await cur.fetchall()]
    assert versions == [1, 2, 3], f"序号必须严格递增，实际 {versions}"


async def test_audit_list_ordered_by_version_for_same_second(db_conn):
    """同秒多次变更 → 列表按 version 倒序（精确到「谁先谁后」，不依赖 created_at）。"""
    from app.services.audit_service import list_prompt_audit_logs

    for i in range(3):
        await db_conn.execute(
            "INSERT INTO prompt_audit_logs"
            " (id,prompt_key,action,before_hash,after_hash,variables_before,variables_after,"
            "client_ip,version,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"id{i}", "K", "update", "a", "b", "[]", "[]", "", 10 + i, "2026-11-09 10:00:00"),
        )
    await db_conn.commit()

    res = await list_prompt_audit_logs(db_conn, "K")
    ids = [i["id"] for i in res["items"]]
    assert ids == ["id2", "id1", "id0"], f"同秒变更必须按 version 倒序，实际 {ids}"
    assert [i["version"] for i in res["items"]] == [12, 11, 10]


async def test_legacy_row_without_version_still_listed(db_conn):
    """旧行（无 version 值）→ version=0 且仍可列出（向后兼容）。"""
    from app.services.audit_service import list_prompt_audit_logs

    await db_conn.execute(
        "INSERT INTO prompt_audit_logs"
        " (id,prompt_key,action,before_hash,after_hash,variables_before,variables_after,client_ip)"
        " VALUES ('legacy','K','update','a','b','[]','[]','')",
    )
    await db_conn.commit()
    res = await list_prompt_audit_logs(db_conn, "K")
    item = next(i for i in res["items"] if i["id"] == "legacy")
    assert item["version"] == 0
    assert item["rollbackable"] is False  # 旧行仍无快照 → 不可回滚（语义不变）


async def test_new_row_sorts_before_legacy_rows(db_conn):
    """新行（version>0）必须排在旧行（version=0）之前。"""
    from app.services.audit_service import list_prompt_audit_logs, record_prompt_audit

    await db_conn.execute(
        "INSERT INTO prompt_audit_logs"
        " (id,prompt_key,action,before_hash,after_hash,variables_before,variables_after,client_ip)"
        " VALUES ('legacy','K','update','a','b','[]','[]','')",
    )
    await record_prompt_audit(db_conn, "K", "update", "b", "a", [], [], None)
    await db_conn.commit()

    res = await list_prompt_audit_logs(db_conn, "K")
    assert res["items"][0]["id"] != "legacy", "新变更必须排在历史行之前"
    assert res["items"][0]["version"] >= 1


async def test_sequence_rollback_leaves_no_gap(db_conn):
    """事务回滚 → 序号与审计行一并回滚（不留空洞、不重复）。"""
    from app.services.audit_service import record_prompt_audit

    await record_prompt_audit(db_conn, "K", "update", "b", "a", [], [], None)
    await db_conn.rollback()

    await record_prompt_audit(db_conn, "K", "update", "b2", "a2", [], [], None)
    await db_conn.commit()

    cur = await db_conn.execute("SELECT version FROM prompt_audit_logs")
    rows = await cur.fetchall()
    assert len(rows) == 1, "回滚的审计行不得残留"
    assert rows[0][0] == 1, "回滚后序号不得留空洞（应重新从 1 起算）"


# ===========================================================================
# L-1 · 知识库素材（逐章量）不得进入「整案稳定段」
# ===========================================================================
def _layered_kwargs(**over):
    kw = dict(
        sys_prompt="S",
        scheme={"name": "n", "type": "t"},
        project_brief="p",
        content_scope="c",
        parent_chain="pc",
        parent_points=[],
        sibling_lines="",
        section_number="3.3",
        leaf={"id": "s1", "title": "降水施工", "description": "d"},
        word_budget=800,
        word_budget_hint="800字",
        prev_sibling_summary="",
        facts_text="F",
        eff_standard="precise",
        knowledge_text="K-A",
    )
    kw.update(over)
    return kw


class TestKnowledgeTextNotInStableSegment:
    """L-1：knowledge_text 逐章不同 ⇒ 稳定段必须仍逐字节相同。

    这是 Prompt Cache 能命中的**直接判据**：显式 cache 断点落在稳定段末尾时，
    段内任一字节不同 ⇒ 整块判不命中 ⇒ 整案上下文每章全价重算。
    """

    def test_stable_segment_identical_when_knowledge_differs(self):
        from app.services.content_runtime import build_chapter_layered_messages

        msgs_a, _ = build_chapter_layered_messages(**_layered_kwargs(knowledge_text="知识-A"))
        msgs_b, _ = build_chapter_layered_messages(
            **_layered_kwargs(
                knowledge_text="知识-B",
                leaf={"id": "s2", "title": "土方开挖", "description": "d2"},
            )
        )
        assert len(msgs_a) == len(msgs_b) == 4
        # system + ①整案稳定上下文 + ②章节树上下文 —— 必须逐字节相同
        assert msgs_a[:-1] == msgs_b[:-1], "知识库素材逐章不同 ⇒ 稳定段/树段仍须逐字节相同"
        assert msgs_a[-1] != msgs_b[-1]

    def test_knowledge_block_not_in_stable_message(self):
        from app.services.content_runtime import build_chapter_layered_messages

        messages, _ = build_chapter_layered_messages(**_layered_kwargs())
        assert "【项目知识库素材】" not in messages[1]["content"], "知识库素材不得污染稳定段"
        assert "【项目知识库素材】" in messages[-1]["content"], "知识库素材必须落在任务差异段"

    def test_knowledge_precedes_current_section(self):
        """R57 顺序契约：知识库素材必须排在【当前章节编号】之前（段首即为此）。"""
        from app.services.content_runtime import build_chapter_layered_messages

        _, user_content = build_chapter_layered_messages(**_layered_kwargs())
        idx_knowledge = user_content.find("【项目知识库素材")
        idx_section = user_content.find("【当前章节编号】")
        assert idx_knowledge >= 0 and idx_section >= 0
        assert idx_knowledge < idx_section, "知识库素材必须排在「当前章节」之前"

    def test_stable_segment_no_longer_contains_knowledge(self):
        """稳定段只剩整案常量（方案/概述/施工内容/事实/标准块）。"""
        from app.services.content_runtime import build_chapter_layered_messages

        messages, _ = build_chapter_layered_messages(**_layered_kwargs())
        stable = messages[1]["content"]
        for label in ("【方案名称】", "【项目概述】", "【全局事实变量"):
            assert label in stable
        assert "【项目知识库素材】" not in stable

