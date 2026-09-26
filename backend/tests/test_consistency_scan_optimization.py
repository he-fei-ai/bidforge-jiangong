"""全文一致性扫描优化（P0-1 · 2026-09-22）回归测试。

实测背景（mock 驱动 12 章全量正文生成）：收尾的全文一致性阶段占总 AI 调用 67.6%，
其中 ``run_scan`` 是**逐章一次 AI 调用且串行**（N 章 = N 次）。三步优化：

  ① 批处理：一次调用扫 k 章（N → ⌈N/k⌉），批结果结构不合法则按章回退；
  ② 并发：批之间并发（Semaphore），只降墙钟；
  ③ 增量：按「章节正文指纹 + 上下文指纹」缓存，改一章只重扫一章。

锁定的不变量：
1. batch_size=1 时与旧行为逐字一致（每章一次 ai_scan_section）；
2. 批结果缺 section_id 或归属到批次外章节 → **整批作废 + 按章回退**（绝不将就）；
3. 缓存只是优化：表缺失 / JSON 损坏 / 查询失败时**退化为全量重扫**，不丢冲突；
4. 正文变更或事实/资料上下文变更 → 该章缓存失效重新扫描；
5. 切批不丢章、不重章（章数与字符数双上限）。
"""
import asyncio
import json

import app.services.consistency_scanner as cs


def _mk_sections(n: int, chars: int = 200):
    return [{"id": f"s{i}", "title": f"第{i}章",
             "content": "基坑深度 18.5m，工期 120 日历天。" * (chars // 20 + 1),
             "sort_order": i} for i in range(n)]


async def _seed(db, n: int = 12):
    secs = _mk_sections(n)
    await db.executemany(
        "INSERT INTO sections (id, scheme_id, title, content, level, sort_order)"
        " VALUES (?,?,?,'',1,?)",
        [(s["id"], "sch1", s["title"], s["sort_order"]) for s in secs])
    await db.execute(
        "UPDATE sections SET content=? WHERE scheme_id='sch1'",
        (secs[0]["content"],))
    await db.executemany(
        "UPDATE sections SET content=? WHERE id=?",
        [(s["content"], s["id"]) for s in secs])
    await db.commit()
    return secs


def _row(topic="檐口高度", value="18.5m"):
    return {"conflict_type": "numeric", "topic": topic, "value": value,
            "text": f"{topic} {value}", "position": 0}


# ============================================================
# ① 切批（纯函数）
# ============================================================
class TestChunkSections:
    def test_batch_size_one_is_legacy_behavior(self):
        secs = _mk_sections(5)
        batches = cs.chunk_sections_for_scan(secs, batch_size=1)
        assert batches == [[s] for s in secs], "batch_size=1 必须每章一批（旧行为）"

    def test_batch_size_groups(self):
        batches = cs.chunk_sections_for_scan(_mk_sections(12), batch_size=4)
        assert [len(b) for b in batches] == [4, 4, 4]

    def test_char_limit_also_cuts(self):
        """字符数是第二道闸门：章数没到但字符超了也要切批。"""
        secs = _mk_sections(4, chars=6000)
        batches = cs.chunk_sections_for_scan(secs, batch_size=10, max_chars=12000)
        assert all(len(b) <= 2 for b in batches)

    def test_no_section_lost_or_duplicated(self):
        secs = _mk_sections(9, chars=3000)
        batches = cs.chunk_sections_for_scan(secs, batch_size=2, max_chars=5000)
        flat = [s["id"] for b in batches for s in b]
        assert flat == [s["id"] for s in secs]

    def test_empty_input(self):
        assert cs.chunk_sections_for_scan([]) == []


# ============================================================
# ① 批结果归属校验
# ============================================================
class TestGroupBatchRows:
    def test_grouped_by_section(self):
        secs = _mk_sections(2)
        rows = [{"section_id": "s0", "topic": "a"}, {"section_id": "s1", "topic": "b"}]
        out = cs._group_batch_rows(rows, secs)
        assert out == {"s0": [rows[0]], "s1": [rows[1]]}

    def test_missing_section_id_invalidates_batch(self):
        """缺 section_id → 整批作废（调用方按章回退）。"""
        assert cs._group_batch_rows([{"topic": "a"}], _mk_sections(2)) is None

    def test_foreign_section_id_invalidates_batch(self):
        assert cs._group_batch_rows([{"section_id": "other", "topic": "a"}],
                                    _mk_sections(2)) is None

    def test_non_dict_row_invalidates_batch(self):
        assert cs._group_batch_rows(["oops"], _mk_sections(1)) is None

    def test_empty_conflicts_is_valid(self):
        assert cs._group_batch_rows([], _mk_sections(2)) == {"s0": [], "s1": []}


# ============================================================
# ①+② 批处理调用次数与回退
# ============================================================
class TestBatchScanCallCount:
    async def test_batch_size_one_uses_single_section_path(self, db_conn, monkeypatch):
        """默认 batch_size=1 时逐章调用（与旧实现一致，零行为变化）。"""
        await _seed(db_conn, 6)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 1)
        single_calls, batch_calls = [], []

        async def fake_single(*, section, facts, project_docs, design_docs, standards):
            single_calls.append(section["id"])
            return []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            batch_calls.append(len(sections))
            return {s["id"]: [] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_section", fake_single)
        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        res = await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                                scheme_name="n", scheme_type="t")
        assert len(single_calls) == 6 and not batch_calls
        assert res["sections"] == 6

    async def test_batch_mode_cuts_calls(self, db_conn, monkeypatch):
        """batch_size=4 时 12 章只需 3 次调用（旧实现 12 次）。"""
        await _seed(db_conn, 12)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        batch_calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            batch_calls.append([s["id"] for s in sections])
            return {s["id"]: [] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        assert len(batch_calls) == 3
        assert [len(b) for b in batch_calls] == [4, 4, 4]

    async def test_batch_failure_falls_back_per_section(self, db_conn, monkeypatch):
        """批调用抛异常 → 按章回退单章扫描，绝不整批判死。"""
        await _seed(db_conn, 4)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        single_calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            raise RuntimeError("AI 挂了")

        async def fake_single(*, section, facts, project_docs, design_docs, standards):
            single_calls.append(section["id"])
            return []

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        monkeypatch.setattr(cs, "ai_scan_section", fake_single)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        assert len(single_calls) == 4

    async def test_invalid_batch_structure_falls_back(self, db_conn, monkeypatch):
        """批结果无法按章归属（缺 section_id）→ 按章回退。"""
        await _seed(db_conn, 4)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        single_calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            return cs._group_batch_rows([_row()], sections)  # 缺 section_id → None

        async def fake_single(*, section, facts, project_docs, design_docs, standards):
            single_calls.append(section["id"])
            return []

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        monkeypatch.setattr(cs, "ai_scan_section", fake_single)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        assert len(single_calls) == 4

    async def test_concurrency_is_bounded(self, db_conn, monkeypatch):
        """批之间并发，且并发数受 Semaphore 约束。"""
        await _seed(db_conn, 12)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 2)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_CONCURRENCY", 3)
        inflight = 0
        peak = 0

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.01)
            inflight -= 1
            return {s["id"]: [] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        assert 1 < peak <= 3, f"并发未生效或超出闸门: peak={peak}"


# ============================================================
# ③ 增量缓存
# ============================================================
class TestIncrementalCache:
    async def test_second_scan_hits_cache(self, db_conn, monkeypatch):
        """内容未变 → 第二次扫描 0 次 AI 调用（改一章只重扫一章的前提）。"""
        await _seed(db_conn, 8)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            calls.append(len(sections))
            return {s["id"]: [_row()] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)

        first = await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                                  scheme_name="n", scheme_type="t")
        assert len(calls) == 2 and first["cached"] == 0

        second = await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                                   scheme_name="n", scheme_type="t")
        assert len(calls) == 2, "第二次不应再调用 AI"
        assert second["cached"] == 8
        # 缓存命中的冲突仍然要产出（不能因为命中缓存就丢了冲突）
        assert second["total"] == first["total"] > 0

    async def test_changed_section_is_rescanned(self, db_conn, monkeypatch):
        """改了一章 → 只重扫这一章，其余命中缓存。"""
        secs = await _seed(db_conn, 8)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        scanned = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            scanned.extend(s["id"] for s in sections)
            return {s["id"]: [_row()] for s in sections}

        async def fake_single(*, section, facts, project_docs, design_docs, standards):
            # 单个章节（不足一批）沿用单章路径
            scanned.append(section["id"])
            return []

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        monkeypatch.setattr(cs, "ai_scan_section", fake_single)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        scanned.clear()

        await db_conn.execute("UPDATE sections SET content=? WHERE id=?",
                              ("改写后的正文：檐口高度 20m。", secs[3]["id"]))
        await db_conn.commit()
        res = await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                                scheme_name="n", scheme_type="t")
        assert scanned == [secs[3]["id"]]
        assert res["cached"] == 7

    async def test_use_cache_false_forces_full_rescan(self, db_conn, monkeypatch):
        await _seed(db_conn, 8)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            calls.append(len(sections))
            return {s["id"]: [] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t", use_cache=False)
        assert len(calls) == 4, "use_cache=False 必须全量重扫"

    async def test_corrupted_cache_degrades_to_full_rescan(self, db_conn, monkeypatch):
        """缓存行 JSON 损坏 → 退化为全量重扫（增量只是优化，绝不丢冲突）。"""
        await _seed(db_conn, 4)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            calls.append(len(sections))
            return {s["id"]: [_row()] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        await db_conn.execute(
            "UPDATE consistency_scan_cache SET rows_json='{坏 JSON'")
        await db_conn.commit()
        calls.clear()
        res = await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                                scheme_name="n", scheme_type="t")
        assert len(calls) == 1 and res["cached"] == 0
        assert res["total"] > 0

    async def test_context_change_invalidates_cache(self, db_conn, monkeypatch):
        """全局事实等上下文变化 → 缓存整体失效（否则会拿旧上下文的结论）。"""
        await _seed(db_conn, 4)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            calls.append(len(sections))
            return {s["id"]: [] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        calls.clear()
        # 上下文（事实/资料）变化：指纹不同 → 全部重扫
        async def _new_facts(db, scheme_id, limit=5000):
            return "新的全局事实：檐口高度 42.5m"

        monkeypatch.setattr(cs, "build_global_facts_text", _new_facts)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t")
        assert len(calls) == 1

    async def test_cache_table_missing_degrades_gracefully(self, db_conn, monkeypatch):
        """缓存表缺失（旧库未迁移）→ 全量重扫，不报错。"""
        await _seed(db_conn, 4)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            calls.append(len(sections))
            return {s["id"]: [_row()] for s in sections}

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        await db_conn.execute("DROP TABLE consistency_scan_cache")
        await db_conn.commit()
        res = await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                                scheme_name="n", scheme_type="t")
        assert len(calls) == 1 and res["cached"] == 0
        assert res["total"] > 0


# ============================================================
# 指纹
# ============================================================
class TestFingerprints:
    def test_content_fingerprint_changes_with_text(self):
        assert cs.content_fingerprint("a") != cs.content_fingerprint("b")
        assert cs.content_fingerprint("") == cs.content_fingerprint("")

    def test_context_fingerprint_covers_all_inputs(self):
        a = cs.context_fingerprint("f", "p", "d", "s")
        assert a != cs.context_fingerprint("f2", "p", "d", "s")
        assert a != cs.context_fingerprint("f", "p2", "d", "s")
        assert a != cs.context_fingerprint("f", "p", "d2", "s")
        assert a != cs.context_fingerprint("f", "p", "d", "s2")

    def test_cached_rows_roundtrip(self):
        rows = cs.normalize_scan_rows([_row()], {"id": "s0", "title": "t"})
        assert json.loads(json.dumps(rows, ensure_ascii=False)) == rows
