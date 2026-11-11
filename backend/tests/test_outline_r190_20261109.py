# -*- coding: utf-8 -*-
"""R190 目录生成模块深度探索收口护栏（2026-11-09）。

四层结构：
  A 行为实证 —— renumber legacy_titles 改名残留（FIX-9）、_is_quota_error
     词边界（FIX-7）、快照回退 word_count 重算（FIX-11）
  B 源码棘轮 —— sse_handlers 终态/窗口/配额/L1 重试（FIX-5/6/7/8）、
     sections BFS 深度（FIX-10）、renormalize word_status（FIX-12）、
     restore 双守卫（FIX-1）、三路径 BEGIN IMMEDIATE（FIX-2）、
     sanitize 覆盖面（FIX-3）、duplicate 守卫（FIX-14）、空标题占位（FIX-15）
  C 提示词编号命名空间硬约束（FIX-16）+ 全角点口径（A-P2-2 附带）
  D 自证 —— 锚点真存在（防 vacuous pass）
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _slice(src: str, start: str, end: str) -> str:
    """取 [start, end) 之间的源码片段（锚点必须唯一，见 D 组自证）。"""
    i = src.index(start)
    j = src.index(end, i + len(start))
    return src[i:j]


def _func_slice(src: str, header: str, next_header: str) -> str:
    return _slice(src, header, next_header)


# ============================================================
# A · 行为实证
# ============================================================
class TestLegacyTitlesRenumber:
    """FIX-9：改名后旧标题自引用行不得翻转占号（其后子标题编号错位一格）。"""

    CONTENT_BASE = "## 措施一\n\n正文。\n\n## 措施二\n"
    CONTENT_WITH_OLD = "## 安全管理\n\n正文。\n\n## 措施一\n\n## 措施二\n"

    @staticmethod
    def _numbers(content: str) -> list[str]:
        out = []
        for line in content.split("\n"):
            m = re.match(r"^#{1,6}\s+(.*)$", line.strip())
            if not m:
                continue
            t = m.group(1)
            if "措施一" in t or "措施二" in t:
                out.append(t)
        return out

    def test_without_legacy_reproduces_shift(self):
        """证伪面（修复前行为）：旧标题行占号 → 措施编号整体 +1 错位。"""
        from app.services.numbering import renumber_section_body_subheadings

        base, _ = renumber_section_body_subheadings(self.CONTENT_BASE, "2.1", 2, "安全管理措施", False)
        bad, _ = renumber_section_body_subheadings(self.CONTENT_WITH_OLD, "2.1", 2, "安全管理措施", False)
        assert self._numbers(base) and self._numbers(bad)
        assert self._numbers(base) != self._numbers(bad), "预置前提失效：旧行为应使编号错位"

    def test_with_legacy_titles_keeps_numbering_and_renames(self):
        """修复后：旧标题行就地改写为当前标题，其后子标题编号与干净基线一致。"""
        from app.services.numbering import renumber_section_body_subheadings

        base, _ = renumber_section_body_subheadings(self.CONTENT_BASE, "2.1", 2, "安全管理措施", False)
        fixed, changes = renumber_section_body_subheadings(
            self.CONTENT_WITH_OLD, "2.1", 2, "安全管理措施", False, legacy_titles=["安全管理"]
        )
        assert self._numbers(fixed) == self._numbers(base), "改名后子标题编号不得错位"
        assert any("安全管理措施" in c.get("new", "") for c in changes), "旧标题自引用行应改写为当前标题"
        assert "安全管理措施" in fixed
        # 旧标题行不再以旧文本独立存在（已被改写/剥离）
        assert not re.search(r"^#{1,6}\s+安全管理\s*$", fixed, re.M)

    def test_leading_old_title_line_stripped(self):
        """正文开头与旧标题重复的块随 legacy 一并剥离（与导出剔除同源）。"""
        from app.services.numbering import renumber_section_body_subheadings

        content = "## 安全管理\n\n## 措施一\n"
        fixed, _ = renumber_section_body_subheadings(content, "2.1", 2, "安全管理措施", False, legacy_titles=["安全管理"])
        assert not re.search(r"^#{1,6}\s+安全管理\s*$", fixed, re.M)

    def test_idempotent(self):
        from app.services.numbering import renumber_section_body_subheadings

        once, _ = renumber_section_body_subheadings(
            self.CONTENT_WITH_OLD, "2.1", 2, "安全管理措施", False, legacy_titles=["安全管理"]
        )
        twice, changes2 = renumber_section_body_subheadings(
            once, "2.1", 2, "安全管理措施", False, legacy_titles=["安全管理"]
        )
        assert once == twice
        assert changes2 == [], "规范化必须幂等（二次执行零改写）"


class TestQuotaErrorWordBoundary:
    """FIX-7：瞬态错误不得因裸子串命中数字码被误判为确定性配额错误。"""

    def test_transient_with_embedded_digits_not_quota(self):
        from app.routers.sse_handlers import _is_quota_error

        assert _is_quota_error("context length 40480 tokens exceeded") is False
        assert _is_quota_error("请求路径 /v1/404xxx 不存在，HTTP 429") is False

    def test_real_quota_errors_still_detected(self):
        from app.routers.sse_handlers import _is_quota_error

        assert _is_quota_error(RuntimeError("HTTP 404: model not found")) is True
        assert _is_quota_error("错误码 404 ，模型不存在") is True
        assert _is_quota_error("账户余额不足") is True
        assert _is_quota_error("unauthorized: invalid api key") is True

    def test_429_never_quota(self):
        from app.routers.sse_handlers import _is_quota_error

        assert _is_quota_error("HTTP 429 rate limited, retry after 3s") is False


class TestRestoreWordCount:
    """FIX-11：快照回退后 schemes.word_count 按恢复后的章节重算。"""

    @staticmethod
    async def _make_db():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = sqlite3.Row
        await db.execute(
            # ✅ R216 重锚：_restore_locked 的 status/outline_source 口径修正后，
            #    真实 schemes 表（schema_sql.py）含 status / outline_source 两列，
            #    桩表须与真实 schema 对齐；INSERT 改显式列名，避免列数不匹配。
            "CREATE TABLE schemes (id TEXT PRIMARY KEY, review_status TEXT, word_count INTEGER,"
            " updated_at TEXT, status TEXT DEFAULT '草稿', outline_source TEXT DEFAULT '')"
        )
        await db.execute(
            "CREATE TABLE sections (id TEXT PRIMARY KEY, scheme_id TEXT, parent_id TEXT,"
            " title TEXT, level INTEGER, sort_order INTEGER, content TEXT, word_count INTEGER, outline_json TEXT)"
        )
        await db.execute(
            "CREATE TABLE scheme_snapshots (id TEXT PRIMARY KEY, scheme_id TEXT, type TEXT,"
            " sections TEXT, created_at TEXT)"
        )
        await db.execute(
            "INSERT INTO schemes (id, review_status, word_count, updated_at)"
            " VALUES ('S', '待审核', 99999, 'old')"
        )
        # 回退目标：两个空正文章节（word_count=0）
        await db.execute(
            "INSERT INTO sections VALUES ('a','S','', '甲', 1, 0, '', 0, '{}'),"
            " ('b','S','a','乙', 2, 0, '', 0, '{}')"
        )
        import json

        rows = [
            dict(r)
            for r in await (await db.execute("SELECT * FROM sections WHERE scheme_id='S'")).fetchall()
        ]
        await db.execute(
            "INSERT INTO scheme_snapshots VALUES ('snap1','S','manual',?, '2026-11-09T00:00:00')",
            (json.dumps({"sections": rows, "review_status": "已审核"}, ensure_ascii=False),),
        )
        # 回退前的「脏」状态：word_count 已膨胀
        await db.execute("UPDATE sections SET word_count=88888 WHERE id='a'")
        await db.commit()
        return db

    @pytest.mark.asyncio
    async def test_word_count_recomputed_after_restore(self):
        from app.services.outline_versions import restore_outline_snapshot

        db = await self._make_db()
        try:
            result = await restore_outline_snapshot(db, "S", "snap1")
            assert result["ok"] is True
            assert result["restored_sections"] == 2
            row = await (await db.execute("SELECT review_status, word_count FROM schemes WHERE id='S'")).fetchone()
            assert row["review_status"] == "已审核"
            assert int(row["word_count"]) == 0, "回退到空正文快照后方案总字数必须归零重算"
        finally:
            await db.close()


# ============================================================
# B · 源码棘轮
# ============================================================
class TestSseHandlersRatchets:
    def test_stopped_not_rewritten_by_checkpoint_failure(self):
        """FIX-5：停止态 checkpoint 写失败 → 终态保持 stopped，仅加法式 checkpoint_failed。"""
        src = _src("routers/sse_handlers.py")
        block = _slice(src, "if not _ckpt_ok:", "await finish_task(task_id, final_status, finish_msg)")
        assert 'final_status = "stopped"' in block
        assert 'final_status = "degraded"' in block
        assert 'payload["checkpoint_failed"] = True' in block
        assert "用户已停止（检查点写入失败" in block

    def test_window_stop_no_longer_discards_completed_units(self):
        """FIX-6：停止后后续单元成果按序并入（不得 break 整体丢弃），且停止后不再空章重试。"""
        src = _src("routers/sse_handlers.py")
        anchor = "for j, (_w_idx, _w_prompts, _w_batch) in enumerate(_window_units):"
        block = _slice(src, anchor, '_prog["nodes"] = _prog.get("nodes", 0) + _added_nodes')
        assert "continue" in block
        assert not re.search(r"^\s*break\s*$", block, re.M), "停止分支不得 break 丢弃后续单元成果"
        assert "if not _stopped_in_unit:" in block, "停止后不得再对后续单元做空章定向重试"

    def test_quota_code_word_boundary(self):
        """FIX-7：数字码走词边界正则，裸数字关键词已移除。"""
        src = _src("routers/sse_handlers.py")
        assert '_QUOTA_CODE_RE = re.compile(r"\\b(?:402|403|404)\\b")' in src
        kw = _slice(src, "_QUOTA_ERROR_KEYWORDS = (", ")")
        for bare in ('"402"', '"403"', '"404"'):
            assert bare not in kw, f"裸数字关键词 {bare} 必须移出 _QUOTA_ERROR_KEYWORDS"

    def test_l1_retry_checks_stop_gate(self):
        """FIX-8：L1 瞬态重试前复检 wait_resume + is_stopped。"""
        src = _src("routers/sse_handlers.py")
        block = _slice(src, "except (asyncio.TimeoutError, RuntimeError, ValueError) as _l1_err:", "obj, _ = await _l1_call()\n                level1")
        assert "await wait_resume(task_id)" in block
        assert "if is_stopped(task_id):" in block
        assert "raise asyncio.CancelledError()" in block


class TestSectionsRatchets:
    def test_restore_snapshot_has_dual_generation_guards(self):
        """FIX-1：快照回退路由必须有正文/目录生成中双 409 守卫。"""
        src = _src("routers/sections.py")
        block = _func_slice(
            src,
            '@router.post("/outline-versions/{snapshot_id}/restore")',
            'from app.services.outline_versions import restore_outline_snapshot as _restore',
        )
        assert "content_generation_in_progress(scheme_id)" in block
        assert "outline_generation_in_progress(scheme_id)" in block
        assert block.count("409") >= 2

    def test_subtree_walk_is_bfs_with_best_depth(self):
        """FIX-10：移动深度校验的子树遍历必须是 BFS + 最深深度重展开（防 DAG 低估）。"""
        src = _src("routers/sections.py")
        block = _slice(src, "_best_depth: dict[str, int] = {}", 'if new_parent in descendants:')
        assert "pending_ids.pop(0)" in block
        assert "pending_ids.pop()" not in block
        assert "_best_depth[r[\"id\"]] = _child_depth" in block
        assert "_walk_cap" in block

    def test_create_section_level_null_guard(self):
        """FIX-13：父 level 列 NULL 时 int(x or 1) 兜底，不再 None+1 抛 500。"""
        src = _src("routers/sections.py")
        assert "level = int(pr[\"level\"] or 1) + 1" in src
        assert 'level = pr["level"] + 1' not in src

    def test_sanitize_covers_create_and_update(self):
        """FIX-3：手工新增/改名两条路径的标题清洗。"""
        src = _src("routers/sections.py")
        # create_section：编号剥离之后必须紧跟清洗
        create_block = _slice(src, "双重编号防御缺口修复", "level = data.level")
        assert "sanitize_outline_title(" in create_block
        # update_section：strip 之后、legacy 捕捉之前完成清洗
        update_block = _slice(
            src,
            'if isinstance(fields.get("title"), str) and fields["title"].strip():',
            'if "title" in fields and _existing_title',
        )
        assert "sanitize_outline_title(" in update_block

    def test_renormalize_updates_word_status(self):
        """FIX-12：结构顺移批量重写正文后 word_status 同步重算。"""
        src = _src("routers/sections.py")
        block = _func_slice(src, "async def _renormalize_all_section_contents", '@router.get("/numbering-consistency")')
        assert "word_count=?, word_status=?" in block
        assert "word_status_for(_wc" in block
        assert "SELECT id, content, word_budget FROM sections" in block

    def test_legacy_titles_wired_in_update_section(self):
        """FIX-9：update_section 旧标题捕捉 + 两处调用点透传。"""
        src = _src("routers/sections.py")
        assert "_legacy_titles = [_existing_title]" in src
        assert src.count("legacy_titles=_legacy_titles") >= 2
        assert "_existing_title" in src


class TestLocksRatchets:
    def test_begin_immediate_on_restore_and_library(self):
        """FIX-2：快照回退 + 目录库 new-version/restore-version 三路径写锁。"""
        ov = _src("services/outline_versions.py")
        assert "BEGIN IMMEDIATE" in ov
        assert "word_count=(SELECT COALESCE(SUM(word_count),0) FROM sections WHERE scheme_id=?)" in ov
        lib = _src("routers/outline_library.py")
        assert lib.count("BEGIN IMMEDIATE") >= 2

    def test_duplicate_scheme_has_generation_guard(self):
        """FIX-14：复制方案前查目录/正文生成中状态。"""
        src = _src("routers/schemes.py")
        block = _func_slice(
            src, '@router.post("/{scheme_id}/duplicate")', "INSERT INTO schemes"
        )
        assert "outline_generation_in_progress(scheme_id)" in block
        assert "content_generation_in_progress(scheme_id)" in block

    def test_create_from_scheme_empty_title_placeholder(self):
        """FIX-15：空标题节点占位重命名，不再连坐丢弃子树。"""
        src = _src("routers/outline_library.py")
        assert "未命名章节" in src
        assert "placeholder_titles_filled" in src
        block = _slice(src, "if not node[\"title\"]:", "out.append(node)")
        assert "node[\"title\"] = " in block

    def test_snapshot_retention_comment_honest(self):
        """C-9：快照保留注释与实现对齐（scheme 全类型共享配额）。"""
        src = _src("services/outline_versions.py")
        assert "全类型共享" in src
        assert "同类型族）" not in src


# ============================================================
# C · 提示词与口径
# ============================================================
class TestPromptNumberingNamespace:
    def test_hard_constraint_on_sublevel_batch_patch(self):
        """FIX-16：sublevel / batch / patch 三模板编号命名空间硬约束齐备。"""
        src = _src("services/ai/prompts/outline.py")
        assert src.count("严禁携带编号前缀") == 3, f"三模板各一处，实得 {src.count('严禁携带编号前缀')}"
        assert "携带即造成双重编号" in src


class TestFullWidthDotCaliber:
    def test_heading_num_prefix_accepts_full_width_dot(self):
        """A-P2-2 附带：同文本判重的编号前缀剥离认全角点「．」（与 _STRIP_NUMBER_RE 对齐）。"""
        from app.services.content_blocks import _HEADING_NUM_PREFIX_RES

        assert "．" in _HEADING_NUM_PREFIX_RES[3].pattern
        assert _HEADING_NUM_PREFIX_RES[3].sub("", "2．1 旧标题") == "旧标题"


# ============================================================
# D · 自证（锚点必须真实存在，防 vacuous pass）
# ============================================================
class TestAnchorsExist:
    def test_all_slice_anchors_present(self):
        sse = _src("routers/sse_handlers.py")
        assert "if not _ckpt_ok:" in sse
        assert "for j, (_w_idx, _w_prompts, _w_batch) in enumerate(_window_units):" in sse
        assert '_prog["nodes"] = _prog.get("nodes", 0) + _added_nodes' in sse
        assert "_QUOTA_ERROR_KEYWORDS = (" in sse
        assert "except (asyncio.TimeoutError, RuntimeError, ValueError) as _l1_err:" in sse
        assert "obj, _ = await _l1_call()\n                level1" in sse

        sec = _src("routers/sections.py")
        assert '@router.post("/outline-versions/{snapshot_id}/restore")' in sec
        assert "from app.services.outline_versions import restore_outline_snapshot as _restore" in sec
        assert "双重编号防御缺口修复" in sec
        assert "level = data.level" in sec
        assert 'fields["title"] = strip_outline_numbering(fields["title"])' in sec
        assert "async def _renormalize_all_section_contents" in sec
        assert '@router.get("/numbering-consistency")' in sec

        sch = _src("routers/schemes.py")
        assert '@router.post("/{scheme_id}/duplicate")' in sch
        assert "INSERT INTO schemes" in sch

        lib = _src("routers/outline_library.py")
        assert 'if not node["title"]:' in lib

    def test_upload_sanitize_present(self):
        """FIX-3：上传保存路径标题清洗在场。"""
        src = _src("routers/upload_outline.py")
        assert "sanitize_outline_title(" in src
        assert "_title_sanitized_n" in src
        assert 'result["title_sanitized"] = _title_sanitized_n' in src
