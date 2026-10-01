"""审核与预检 + 导出文档模块缺陷修复护栏（2026-09-30 第八轮）。

覆盖 3 类修复，全部默认向后兼容（无新配置项、无行为开关）：

1. **【P2 评审可追溯性】``review_checklist`` 最近评审记录的确定性排序。**
   review_records.created_at 精度只到秒（DB 默认 datetime('now','localtime')），
   批量审核 / 正文重生成重置会在同一秒写入多条。旧 SQL 只按 created_at DESC ——
   并列时由 SQLite 执行计划决定（是否走索引反向扫描不确定），"最近意见"可能
   显示成同一秒内较早写入的那条。补 rowid DESC 兜底（与 /summary、/records
   两端已采用的同口径），使"最近 = 最后写入"恒成立。

2. **【P1 残缺文档被写入缓存 → 永久缺图（第二缺口）】``_count_undownloaded_image_blocks``。**
   ``_ai_pending`` 只统计「生图失败后残留的 ai_image 占位块」；生图**成功**但
   位图下载失败（_fetch_image 失败 / 厂商返回非图片）的 image 块此前不计入
   坏缓存守卫 → 成稿缺图却被 INSERT 进 export_cache。新纯函数按与
   write_section image 分支**同口径**统计未取回位图的 image 块，由
   ``_prepare_export`` 计入 ``render_stats`` / ``prep``，export_docx 坏缓存
   守卫一并拦截。

3. **【P1 R13 漏改点】``placeholder_history`` / ``cache_status`` 的 db.execute
   判空。** 全局单连接下 execute 可能返回 None；两处均为只读旁路，降级返回
   空结果而非 500。
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

import app.db as _appdb
from app.db import get_conn, init_db, close_db
from app.routers.export import (
    _count_undownloaded_image_blocks,
    cache_status,
    placeholder_history,
)
from app.routers.review import review_checklist


@pytest.fixture
async def db_ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "review-export-fixes.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "s", "目录已确认"))
    await db.commit()
    yield db, pid, sid
    await close_db()


async def _insert_section(db, sid, sec_id=None, title="第一章"):
    sec_id = sec_id or uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, "", "", title, "", 1, "empty", 0, 0, "正文", "pending", 0))
    await db.commit()
    return sec_id


# ---------------------------------------------------------------------------
# 1. review_checklist 最近评审记录确定性排序（同秒并列 → rowid DESC 兜底）
# ---------------------------------------------------------------------------
class TestReviewChecklistLatestTieBreak:
    async def _insert_record(self, db, sid, sec_id, comment, created_at):
        await db.execute(
            "INSERT INTO review_records (id, scheme_id, project_id, section_id,"
            " section_title, from_status, to_status, reviewer, comment, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, "", sec_id, "章一",
             "pending", "approved", "评审人", comment, created_at))
        await db.commit()

    async def test_latest_is_last_written_in_same_second(self, db_ctx):
        """同一秒内先通过后驳回：最近意见必须显示「驳回」那条（rowid 大的）。"""
        db, _pid, sid = db_ctx
        sec_id = await _insert_section(db, sid)
        same_ts = datetime.now().isoformat(timespec="seconds")
        await self._insert_record(db, sid, sec_id, "首条-通过", same_ts)
        await self._insert_record(db, sid, sec_id, "次条-驳回", same_ts)

        result = await review_checklist(sid, db)
        items = {i["id"]: i for i in result["items"]}
        # 最近意见 = 同一秒内**最后写入**的记录（rowid 更大），而非执行计划任意取一条
        assert items[sec_id]["last_comment"] == "次条-驳回"

    async def test_diff_second_latest_wins(self, db_ctx):
        """跨秒记录：时间新者优先（rowid 兜底不破坏既有语义）。"""
        db, _pid, sid = db_ctx
        sec_id = await _insert_section(db, sid)
        await self._insert_record(db, sid, sec_id, "较早-通过", "2026-09-01T08:00:00")
        await self._insert_record(db, sid, sec_id, "较新-驳回", "2026-09-02T08:00:00")

        result = await review_checklist(sid, db)
        items = {i["id"]: i for i in result["items"]}
        assert items[sec_id]["last_comment"] == "较新-驳回"


# ---------------------------------------------------------------------------
# 2. 坏缓存守卫第二缺口：生图成功但位图下载失败的 image 块统计
# ---------------------------------------------------------------------------
class TestCountUndownloadedImageBlocks:
    def _mk(self):
        blocks_cache = {
            "s1": [
                {"type": "image", "url": "https://img.example.com/a.png", "alt": "图A"},
                {"type": "image", "url": "https://img.example.com/b.png", "alt": "图B"},
                {"type": "ai_image", "url": "https://img.example.com/c.png"},  # 占位块不统计
            ],
            "s2": [
                {"type": "image", "url": "", "alt": "空URL"},  # 空 URL 不统计（与 image 分支一致）
                {"type": "paragraph", "text": "正文"},
            ],
            "s3": [],  # 空章
        }
        sections = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
        return sections, blocks_cache

    def test_counts_only_missing_downloads(self):
        sections, blocks_cache = self._mk()
        # a.png 已取回，b.png 未取回 → 只统计 b
        image_bytes = {"https://img.example.com/a.png": object()}
        assert _count_undownloaded_image_blocks(sections, blocks_cache, image_bytes) == 1

    def test_all_downloaded_is_zero(self):
        sections, blocks_cache = self._mk()
        image_bytes = {
            "https://img.example.com/a.png": object(),
            "https://img.example.com/b.png": object(),
        }
        assert _count_undownloaded_image_blocks(sections, blocks_cache, image_bytes) == 0

    def test_empty_downloads_counts_missing(self):
        """image_bytes 为空（下载全部失败）时，正文里的 image 块都要被跳过 → 必须计数。

        这正是坏缓存守卫要拦的场景：生图成功、下载全败 → 成稿缺图却入缓存。"""
        sections, blocks_cache = self._mk()
        # s1 有 2 张 image（a/b），s2 的 image 为空 URL（不计）→ 计 2
        assert _count_undownloaded_image_blocks(sections, blocks_cache, {}) == 2
        assert _count_undownloaded_image_blocks(sections, blocks_cache, None) == 2

    def test_missing_block_entry_tolerated(self):
        """blocks_cache 缺某章的键 → 跳过不崩（判据须与 write_section 同容错）。"""
        sections = [{"id": "s1"}, {"id": "no_cache"}]
        blocks_cache = {
            "s1": [{"type": "image", "url": "https://img.example.com/x.png"}],
        }
        image_bytes = {}
        # x.png 未下载 → 计 1；no_cache 章无缓存 → 跳过
        assert _count_undownloaded_image_blocks(sections, blocks_cache, image_bytes) == 1


# ---------------------------------------------------------------------------
# 3. R13 漏改点：placeholder_history / cache_status 的 db.execute 判空
# ---------------------------------------------------------------------------
class _NoneDb:
    """模拟 db.execute 恒返回 None（连接/事务异常路径）。"""

    async def execute(self, sql, params=None):  # noqa: ANN001
        return None


class TestExportReadEndpointR13Guard:
    async def test_placeholder_history_cur_none_degrades_to_empty(self, db_ctx):
        _db, _pid, sid = db_ctx
        result = await placeholder_history(sid, 20, _NoneDb())
        assert result["history"] == []
        assert result["scheme_id"] == sid

    async def test_cache_status_cur_none_degrades_to_empty(self, db_ctx):
        result = await cache_status(uuid.uuid4().hex, _NoneDb())
        assert result["items"] == []
        assert result["total"] == 0
        assert result["stale"] == 0

    async def test_placeholder_history_normal_path(self, db_ctx):
        """正常路径不受守卫影响：落一行基线后可查回。"""
        db, _pid, sid = db_ctx
        await db.execute(
            "INSERT INTO placeholder_baselines (id, scheme_id, total, formatted_total,"
            " bare_total, fuzzy_total, field_count, section_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, 3, 1, 1, 1, 2, 1))
        await db.commit()
        result = await placeholder_history(sid, 20, db)
        assert result["history"]
        assert result["history"][0]["total"] == 3