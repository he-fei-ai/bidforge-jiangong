"""定向修复阶段降本（2026-09-22）回归测试。

扫描优化落地后，定向修复成为正文生成收尾的新大头（12 章方案占 42.9%
= 涉及章节数 × 1~2 次调用，且旧实现逐章**串行**）。四项优化：

  ① 并发：逐章串行 → Semaphore 并发（只降墙钟，调用数不变）；
  ② 跳过「历史已修复且修复成果仍在正文中」的冲突（避免同一处反复花钱修）；
  ③ 单次修复章节数上限（长方案分批修，超出按严重度排序跳过）；
  ④ 重试收敛：仅 AI 调用异常才重试，校验不合格默认不再重试第二次
     （一致性_repair_retry_on_invalid 可恢复旧行为）。

锁定的不变量：
1. 跳过与上限只影响「要不要调用」，不改修复/校验/落库语义；
2. 上限与跳过都必须产出 skipped 条目（前端能看见原因，且 repaired+failed+skipped == total）；
3. AI 调用异常（超时/网络）仍然重试一次 —— 瞬时故障不该直接判失败；
4. 历史修复记录读取失败 → 退化为「不跳过」（只是少省一次调用，不阻断）。
"""
import asyncio

import app.services.repair_agent as ra


def _conflict(cid: str, topic: str = "檐口高度", sid: str = "s0",
              value: str = "18.5m", auth: str = "42.5m",
              severity: str = "high") -> dict:
    return {
        "id": cid, "conflict_type": "numeric", "severity": severity,
        "topic": topic, "authoritative_value": auth,
        "authoritative_source": "全局事实", "status": "pending",
        "occurrences": [{"section_id": sid, "section_title": f"章节{sid}",
                         "value": value, "text": f"{topic} {value}"}],
    }


async def _seed_sections(db, n: int = 3, content: str = "原文：檐口高度 18.5m。"):
    for i in range(n):
        await db.execute(
            "INSERT INTO sections (id, scheme_id, title, content, level, sort_order)"
            " VALUES (?,?,?,?,2,?)",
            (f"s{i}", "sch1", f"章节s{i}", content, i))
    await db.commit()


def _patch_repair(monkeypatch, after="修复后正文：檐口高度 42.5m。", ok=True,
                  err=None, delay: float = 0.0):
    calls = []

    async def fake_repair_section(*, section_id, section_title, section_content,
                                  conflicts_in_section, facts, sources):
        calls.append(section_id)
        if delay:
            await asyncio.sleep(delay)
        if err is not None:
            raise err
        return after

    monkeypatch.setattr(ra, "repair_section", fake_repair_section)
    monkeypatch.setattr(ra, "validate_repair",
                        lambda **kw: (ok, [] if ok else ["仍含错误值"]))
    return calls


# ============================================================
# ② 跳过「已修复且成果仍在」（纯函数）
# ============================================================
class TestFilterAlreadyRepaired:
    def test_skips_when_fix_still_present(self):
        content = "修复后正文：檐口高度 42.5m。"
        marks = {("s1", "檐口高度", "修复后正文")}
        todo, skipped = ra.filter_already_repaired(
            "s1", [{"topic": "檐口高度"}], content, marks)
        assert not todo and len(skipped) == 1

    def test_does_not_skip_other_topic(self):
        marks = {("s1", "檐口高度", "修复后正文")}
        todo, skipped = ra.filter_already_repaired(
            "s1", [{"topic": "基坑深度"}], "修复后正文：檐口高度 42.5m。", marks)
        assert len(todo) == 1 and not skipped

    def test_does_not_skip_other_section(self):
        marks = {("s1", "檐口高度", "修复后正文")}
        todo, _ = ra.filter_already_repaired(
            "s2", [{"topic": "檐口高度"}], "修复后正文：檐口高度 42.5m。", marks)
        assert len(todo) == 1

    def test_does_not_skip_when_fix_overwritten(self):
        """修复成果被后续生成覆盖（after 片段不在正文里）→ 必须重修。"""
        marks = {("s1", "檐口高度", "修复后正文")}
        todo, skipped = ra.filter_already_repaired(
            "s1", [{"topic": "檐口高度"}], "重新生成的正文：檐口高度 18.5m。", marks)
        assert len(todo) == 1 and not skipped

    def test_empty_marks_keeps_everything(self):
        todo, skipped = ra.filter_already_repaired("s1", [{"topic": "a"}], "x", set())
        assert len(todo) == 1 and not skipped


class TestLoadRepairedMarks:
    async def test_reads_history(self, db_conn):
        await db_conn.execute(
            "INSERT INTO consistency_repairs (id, scheme_id, scan_id, mode,"
            " total_conflicts, repaired, skipped, failed, items, snapshot_id,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", "sch1", "scan1", "auto", 1, 1, 0, 0,
             '[{"section_id":"s1","topic":"檐口高度","status":"repaired",'
             '"after":"修复后正文：檐口高度 42.5m。"}]',
             "ver1", "pending_confirm", "2026-09-22 10:00:00"))
        await db_conn.commit()
        marks = await ra.load_repaired_marks(db_conn, "sch1")
        assert any(m[0] == "s1" and m[1] == "檐口高度" for m in marks)

    async def test_ignores_failed_history(self, db_conn):
        await db_conn.execute(
            "INSERT INTO consistency_repairs (id, scheme_id, scan_id, mode,"
            " total_conflicts, repaired, skipped, failed, items, snapshot_id,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", "sch1", "scan1", "auto", 1, 0, 0, 1,
             '[{"section_id":"s1","topic":"檐口高度","status":"failed","after":"x"}]',
             "ver1", "pending_confirm", "2026-09-22 10:00:00"))
        await db_conn.commit()
        assert await ra.load_repaired_marks(db_conn, "sch1") == set()

    async def test_broken_history_degrades_to_empty(self, db_conn):
        await db_conn.execute(
            "INSERT INTO consistency_repairs (id, scheme_id, scan_id, mode,"
            " total_conflicts, repaired, skipped, failed, items, snapshot_id,"
            " status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", "sch1", "scan1", "auto", 1, 1, 0, 0, "{坏 JSON", "ver1",
             "pending_confirm", "2026-09-22 10:00:00"))
        await db_conn.commit()
        assert await ra.load_repaired_marks(db_conn, "sch1") == set()


# ============================================================
# ③ 单次修复章节数上限（纯函数）
# ============================================================
class TestLimitRepairSections:
    def _work(self, specs):
        return [(sid, {"section_title": sid, "conflicts": [
            {"severity": sev} for _ in range(n)]})
            for sid, sev, n in specs]

    def test_no_limit_keeps_all(self):
        work = self._work([("s0", "high", 1), ("s1", "high", 1), ("s2", "high", 1)])
        kept, over = ra.limit_repair_sections(work, 0)
        assert len(kept) == 3 and not over

    def test_keeps_most_severe_first(self):
        work = self._work([("s0", "low", 1), ("s1", "high", 1), ("s2", "medium", 1)])
        kept, over = ra.limit_repair_sections(work, 2)
        assert [k[0] for k in kept] == ["s1", "s2"]
        assert [k[0] for k in over] == ["s0"]

    def test_more_conflicts_wins_on_tie(self):
        work = self._work([("s0", "high", 1), ("s1", "high", 3)])
        kept, over = ra.limit_repair_sections(work, 1)
        assert [k[0] for k in kept] == ["s1"]


# ============================================================
# ① 并发 / ②④ 端到端
# ============================================================
class TestRunRepairOptimizations:
    async def test_already_repaired_conflict_is_skipped(self, db_conn, monkeypatch):
        """第二次修复：修复成果仍在正文里 → 不再调用 AI。"""
        await _seed_sections(db_conn, 1)
        calls = _patch_repair(monkeypatch)
        conflict = _conflict("C001")

        first = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                                    conflicts=[conflict], mode="auto")
        assert first["repaired"] == 1 and len(calls) == 1

        calls.clear()
        second = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan2",
                                     conflicts=[_conflict("C002")], mode="auto")
        assert len(calls) == 0, "已修复且成果仍在，不应再花 AI 调用"
        assert second["repaired"] == 0 and second["skipped"] == 1
        assert any("已修复" in str(i.get("problems"))
                   for i in second["items"]), "跳过原因必须可见"

    async def test_force_full_repair_repairs_everything(self, db_conn, monkeypatch):
        """force_full_repair=true：关闭跳过优化，已修复的也重新修一遍。"""
        await _seed_sections(db_conn, 1)
        calls = _patch_repair(monkeypatch)
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001")], mode="auto")
        calls.clear()
        res = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan2",
                                  conflicts=[_conflict("C002")], mode="auto",
                                  force_full_repair=True)
        assert len(calls) == 1, "强制全量修复时必须重新调用 AI"
        assert res["repaired"] == 1 and res["skipped"] == 0

    async def test_force_full_repair_false_is_default(self, db_conn, monkeypatch):
        """不传该参数 = 跳过优化生效（默认省调用）。"""
        await _seed_sections(db_conn, 1)
        calls = _patch_repair(monkeypatch)
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001")], mode="auto")
        calls.clear()
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan2",
                            conflicts=[_conflict("C002")], mode="auto",
                            force_full_repair=False)
        assert len(calls) == 0

    async def test_repaired_then_overwritten_is_repaired_again(self, db_conn, monkeypatch):
        """正文被后续生成覆盖（旧修复成果不在）→ 必须重修，不能误跳过。"""
        await _seed_sections(db_conn, 1)
        calls = _patch_repair(monkeypatch)
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001")], mode="auto")
        # 模拟「重新生成正文」覆盖了修复成果
        await db_conn.execute("UPDATE sections SET content=? WHERE id='s0'",
                              ("重新生成的正文：檐口高度 18.5m。",))
        await db_conn.commit()
        calls.clear()
        res = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan2",
                                  conflicts=[_conflict("C002")], mode="auto")
        assert len(calls) == 1 and res["repaired"] == 1

    async def test_max_sections_skips_overflow(self, db_conn, monkeypatch):
        await _seed_sections(db_conn, 3)
        monkeypatch.setattr(ra, "REPAIR_MAX_SECTIONS", 2)
        calls = _patch_repair(monkeypatch)
        conflicts = [_conflict(f"C00{i}", sid=f"s{i}", severity=sev)
                     for i, sev in enumerate(["low", "high", "medium"])]
        res = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                                  conflicts=conflicts, mode="auto",
                                  severity_threshold="low")
        assert len(calls) == 2
        assert sorted(calls) == ["s1", "s2"], "应按严重度保留 high/medium"
        skipped = [i for i in res["items"] if i["status"] == "skipped"]
        assert skipped and "上限" in str(skipped[0]["problems"])

    async def test_concurrency_is_bounded(self, db_conn, monkeypatch):
        await _seed_sections(db_conn, 6)
        monkeypatch.setattr(ra, "REPAIR_CONCURRENCY", 3)
        calls = _patch_repair(monkeypatch, delay=0.01)
        conflicts = [_conflict(f"C00{i}", sid=f"s{i}") for i in range(6)]
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=conflicts, mode="auto")
        assert len(calls) == 6

    async def test_invalid_output_is_not_retried_by_default(self, db_conn, monkeypatch):
        """④ 校验不合格：默认不重试（省 1 次调用/失败章节）。

        ✅ 2026-09-22（O8）行为修订：组内含 **high 级冲突**时校验不过仍重试
        一次（省重试会让高危冲突的错误参数留在交付文档里）。本用例锁定
        「分级开关关闭时维持不重试」的旧行为；high 级默认重试见
        tests/test_callopt_batch3.py::TestSeverityGradedRetry。
        """
        await _seed_sections(db_conn, 1)
        monkeypatch.setattr(ra, "REPAIR_RETRY_ON_INVALID", False)
        monkeypatch.setattr(ra, "REPAIR_RETRY_ON_INVALID_BY_SEVERITY", False)
        calls = _patch_repair(monkeypatch, ok=False)
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001")], mode="auto")
        assert len(calls) == 1

    async def test_invalid_output_retry_can_be_enabled(self, db_conn, monkeypatch):
        monkeypatch.setattr(ra, "REPAIR_RETRY_ON_INVALID", True)
        await _seed_sections(db_conn, 1)
        calls = _patch_repair(monkeypatch, ok=False)
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001")], mode="auto")
        assert len(calls) == 2, "打开开关后应恢复旧行为（重试一次）"

    async def test_ai_exception_still_retries_once(self, db_conn, monkeypatch):
        """AI 调用异常（超时/网络）必须重试一次 —— 瞬时故障不该直接判失败。"""
        await _seed_sections(db_conn, 1)
        calls = _patch_repair(monkeypatch, err=RuntimeError("HTTP 504: timeout"))
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001")], mode="auto")
        assert len(calls) == 2

    async def test_progress_callback_reaches_total(self, db_conn, monkeypatch):
        await _seed_sections(db_conn, 3)
        _patch_repair(monkeypatch)
        seen = []

        async def _cb(done, total, message):
            seen.append((done, total))

        conflicts = [_conflict(f"C00{i}", sid=f"s{i}") for i in range(3)]
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=conflicts, mode="auto", progress_cb=_cb)
        assert (3, 3) in seen, "进度必须推进到 total（前端据此收尾）"
