"""全文一致性修复链路 · 接线与字数口径回归测试（2026-09-16）

依据运行库真实记录定位的三处缺陷（修复后由本文件守护）：

| 缺陷 | 运行库证据 | 后果 |
|---|---|---|
| `repair_section()` 调用漏传必填关键字参数 `section_id` | `consistency_repairs.repaired=0 / failed=13`，13 条 problems 全为 `TypeError: missing 1 required keyword-only argument` | 「自动一致性修复」自签名变更以来**从未生效** |
| 修复写库用 `word_count=len(after)` 且不更新 `word_status` | 运行库 3 章 `word_count` 含图表代码（「进度计划横道图与网络图」实际 1718 字被判 2608 字 → 假 over） | 字数口径与全项目唯一口径脱节，误导用户压缩 |
| 修复统计按「章节×冲突」计数而 total 按冲突计数 | `total_conflicts=9` 且 `failed=13` 同批记录 | 界面出现"发现 9 处、失败 13 处"的矛盾数字 |

本文件用**真库（in-memory schema）+ 假 AI** 端到端跑 `run_repair`，
调用的是未打桩的 `repair_section`（签名即护栏），因此第一条缺陷会被直接暴露。
"""
import inspect
import json

import pytest
from app.services import repair_agent, repair_record
from app.services.content_utils import text_word_count, word_status_for

SCHEME = "sch-1"
PROJ = "prj-1"
SEC = "sec-1"

_BEFORE = ("本工程总工期为 120 日历天，混凝土强度等级为 C30。"
           "架体搭设高度 50 m，扣件螺栓拧紧力矩 40~65N·m。\n\n"
           "```mermaid\ngraph TD; A[开工]-->B[竣工]\n```\n")
# 修复后：错误取值 120 → 权威值 90，其余保持不变（通过 validate_repair 各条校验）
_AFTER = ("本工程总工期为 90 日历天，混凝土强度等级为 C30。"
          "架体搭设高度 50 m，扣件螺栓拧紧力矩 40~65N·m。\n\n"
          "```mermaid\ngraph TD; A[开工]-->B[竣工]\n```\n")
# 落库正文是 `_clean_repair_output` 后的形态（首尾空白被 strip）
_AFTER_CLEAN = _AFTER.strip()


async def _seed(db, *, budget: int = 50, content: str = _BEFORE) -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PROJ, "P"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (SCHEME, PROJ, "S"))
    await db.execute(
        "INSERT INTO sections(id,scheme_id,project_id,title,content,word_count,"
        "word_status,word_budget,status,level,sort_order) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (SEC, SCHEME, PROJ, "工程概况", content, text_word_count(content),
         word_status_for(text_word_count(content), budget), budget,
         "generated", 1, 0))
    await db.commit()


def _conflict(cid: str, *, value: str = "120 日历天", severity: str = "high",
              auth: str = "90 日历天") -> dict:
    return {
        "id": cid, "scheme_id": SCHEME, "scan_id": "S1",
        "conflict_type": "numeric", "severity": severity, "topic": "项目总工期",
        "occurrences": [{"section_id": SEC, "section_title": "工程概况",
                         "text": "本工程总工期为 120 日历天", "position": 0,
                         "value": value, "conflict_id": cid}],
        "authoritative_value": auth, "authoritative_source": "全局事实变量 · 工期",
        "repair_instruction": f"将「{value}」改为「{auth}」",
        "reason": "全局事实变量明确工期", "status": "pending",
    }


class TestRepairCallSite:
    """R-1：调用点必须传 section_id（签名即护栏）。"""

    def test_signature_requires_section_id(self):
        params = inspect.signature(repair_agent.repair_section).parameters
        assert "section_id" in params
        assert params["section_id"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["section_id"].default is inspect.Parameter.empty

    def test_call_site_passes_section_id(self):
        src = inspect.getsource(repair_agent.run_repair)
        assert "section_id=sid" in src

    async def test_run_repair_end_to_end_updates_content(self, db_conn, monkeypatch):
        await _seed(db_conn)
        seen: list[list] = []

        async def fake_chat(messages, **kwargs):
            seen.append(messages)
            return _AFTER

        monkeypatch.setattr(repair_agent, "chat_with_fallback", fake_chat)

        res = await repair_agent.run_repair(
            db_conn, scheme_id=SCHEME, scan_id="S1",
            conflicts=[_conflict("C1")], severity_threshold="high")

        assert seen, "修复必须真正调用 AI（旧实现因漏传 section_id 直接 TypeError）"
        assert res["repaired"] == 1, res
        assert res["failed"] == 0
        assert res["items"][0]["status"] == "repaired"
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC,))
        assert (await cur.fetchone())[0] == _AFTER_CLEAN


class TestRepairWordCountConsistency:
    """R-4：修复落库必须用全项目唯一字数口径，并同步 word_status。"""

    async def test_word_count_and_status_recomputed(self, db_conn, monkeypatch):
        await _seed(db_conn, budget=50)

        async def fake_chat(messages, **kwargs):
            return _AFTER

        monkeypatch.setattr(repair_agent, "chat_with_fallback", fake_chat)
        await repair_agent.run_repair(
            db_conn, scheme_id=SCHEME, scan_id="S1",
            conflicts=[_conflict("C1")], severity_threshold="high")

        cur = await db_conn.execute(
            "SELECT content, word_count, word_status FROM sections WHERE id=?", (SEC,))
        row = await cur.fetchone()
        expect = text_word_count(_AFTER_CLEAN)
        assert row[1] == expect, "word_count 必须剔除```图表代码块"
        assert row[1] != len(_AFTER_CLEAN), "不得用 len(content)（含图表代码）"
        assert row[2] == word_status_for(expect, 50)

    def test_chart_code_does_not_inflate_word_count(self):
        with_fence = "正文" * 40 + "\n```mermaid\ngraph TD; A-->B\n```\n"
        assert text_word_count(with_fence) < len(with_fence)


class TestRepairStatistics:
    """R-2：统计口径统一（按冲突计数，且三项之和恒等于冲突总数）。"""

    async def test_counts_are_conflict_based_and_exhaustive(self, db_conn, monkeypatch):
        await _seed(db_conn)

        async def fake_chat(messages, **kwargs):
            return _AFTER

        monkeypatch.setattr(repair_agent, "chat_with_fallback", fake_chat)

        conflicts = [
            _conflict("C1"),                                      # high → 修复
            _conflict("C2", value="150 日历天", severity="low"),   # 低于阈值 → 不修
            {"id": "C3", "scheme_id": SCHEME, "scan_id": "S1",
             "conflict_type": "numeric", "severity": "high", "topic": "无权威值",
             "occurrences": [{"section_id": SEC, "section_title": "工程概况",
                              "text": "x", "position": 0, "value": "x"}],
             "authoritative_value": "", "authoritative_source": "",
             "repair_instruction": "", "reason": "", "status": "pending"},
        ]
        res = await repair_agent.run_repair(
            db_conn, scheme_id=SCHEME, scan_id="S1", conflicts=conflicts,
            severity_threshold="high")

        assert res["total_conflicts"] == 3
        assert res["repaired"] == 1
        assert res["skipped"] == 2                 # 低于阈值 1 + 无权威值 1
        assert res["failed"] == 0
        assert res["repaired"] + res["failed"] + res["skipped"] == res["total_conflicts"]

    async def test_save_repair_accepts_explicit_stats(self, db_conn):
        await _seed(db_conn)
        out = await repair_record.save_repair(
            db_conn, scheme_id=SCHEME, scan_id="S1", mode="auto",
            items=[{"conflict_id": "C1", "status": "repaired"},
                   {"conflict_id": "C1", "status": "repaired"},   # 同冲突出现在两章
                   {"conflict_id": "C2", "status": "failed"}],
            snapshot_id="ver_x", total_conflicts=2,
            stats={"repaired": 1, "failed": 1, "skipped": 0})
        assert (out["repaired"], out["failed"], out["skipped"]) == (1, 1, 0)


class TestRollbackWordCount:
    """R-5：一键回滚同样必须重算字数与状态。"""

    async def test_rollback_recomputes_word_count(self, db_conn):
        await _seed(db_conn, budget=50, content=_AFTER_CLEAN)
        snap = await repair_record.create_snapshot(
            db_conn, SCHEME, [{"section_id": SEC, "content_before": _BEFORE}])
        # 模拟"修复后"的库状态（字数被写成含图表代码的长度）
        await db_conn.execute(
            "UPDATE sections SET word_count=?, word_status='over' WHERE id=?",
            (len(_AFTER_CLEAN), SEC))
        await db_conn.commit()

        await repair_record.rollback_snapshot(db_conn, snap)

        cur = await db_conn.execute(
            "SELECT content, word_count, word_status FROM sections WHERE id=?", (SEC,))
        row = await cur.fetchone()
        assert row[0] == _BEFORE                    # 快照原文原样恢复
        expect = text_word_count(_BEFORE)
        assert row[1] == expect
        assert row[2] == word_status_for(expect, 50)


class TestConfirmRejectWordCount:
    """R-6：/confirm 拒绝某冲突（章节整体恢复快照原文）后，
    word_count / word_status 必须按全项目唯一口径重算。

    根因：consistency_repair.confirm 的旧实现只写 content，
    字数与状态停留在**修复后**的值 —— 用户拒绝修复 → 正文回到快照原文
    （可能更短）→ 库里却仍显示修复后更长的字数与假 over，触发误导压缩。
    与 repair_agent.run_repair / rollback_snapshot 同类字数口径缺陷。
    """

    async def test_confirm_reject_recomputes_word_count(self, db_conn, monkeypatch):
        from app.routers.consistency_repair import confirm

        await _seed(db_conn, budget=50, content=_BEFORE)
        # 制造"修复后"落库状态：正文被改成更长，字数被写成 len(after)（污染）
        await db_conn.execute(
            "UPDATE sections SET content=?, word_count=?, word_status='over' "
            "WHERE id=?", (_AFTER_CLEAN, len(_AFTER_CLEAN), SEC))
        await db_conn.commit()

        # 快照 + 修复批次记录
        snap = await repair_record.create_snapshot(
            db_conn, SCHEME, [{"section_id": SEC, "content_before": _BEFORE}])
        rep = await repair_record.save_repair(
            db_conn, scheme_id=SCHEME, scan_id="S1", mode="auto",
            items=[{"conflict_id": "C1", "section_id": SEC, "status": "repaired",
                    "problems": []}],
            snapshot_id=snap, total_conflicts=1,
            stats={"repaired": 1, "failed": 0, "skipped": 0})

        # 通过 /confirm 拒绝该冲突 —— 章节应整体恢复快照原文
        result = await confirm(
            scheme_id=SCHEME,
            body={"repair_id": rep["repair_id"], "accepted": [], "rejected": ["C1"]},
            db=db_conn)
        assert result["status"] == "rejected"
        assert result["restored_sections"] == [SEC]

        cur = await db_conn.execute(
            "SELECT content, word_count, word_status FROM sections WHERE id=?", (SEC,))
        row = await cur.fetchone()
        expect = text_word_count(_BEFORE)
        assert row[0] == _BEFORE, "拒绝后应恢复快照原文"
        assert row[1] == expect, (
            "拒绝后 word_count 必须剔除```图表代码块，不得停留在修复后 len(content)")
        assert row[2] == word_status_for(expect, 50), (
            "拒绝后 word_status 必须与恢复后的正文一致，不得假 over")


class TestRepairFailureKeepsOriginal:
    """修复失败必须保留原文，并把原因写进 items（旧实现只留下 TypeError 原文）。"""

    async def test_failure_records_problem_text(self, db_conn, monkeypatch):
        await _seed(db_conn)

        async def boom(messages, **kwargs):
            raise RuntimeError("模型不可用")

        monkeypatch.setattr(repair_agent, "chat_with_fallback", boom)
        res = await repair_agent.run_repair(
            db_conn, scheme_id=SCHEME, scan_id="S1",
            conflicts=[_conflict("C1")], severity_threshold="high")
        assert res["failed"] == 1
        assert "模型不可用" in json.dumps(res["items"], ensure_ascii=False)
        cur = await db_conn.execute("SELECT content FROM sections WHERE id=?", (SEC,))
        assert (await cur.fetchone())[0] == _BEFORE

