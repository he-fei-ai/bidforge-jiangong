"""性能瓶颈回归测试（配套《性能瓶颈分析与优化方案.md》）

锁定 P0/P1 瓶颈所依赖的正确性不变量，防止后续优化引入回归：
- ChartCache：命中/统计、跨类型 key 隔离、LRU 容量约束、clear 重置、相同 key 只渲染一次
- provider_factory._fallback_chain：优先级排序 + 主配置排除（优化为缓存后语义不变）
- sse_handlers._apply_word_budget_allocations：字数守恒 / AI 分配采纳
- sse_handlers._build_parent_chain：预构建索引路径与重建路径结果一致（防 O(N²) 回归）
- export 纯函数：内容块解析 / 标题编号剥离 / 中文数字解析 / 代码归一化（导出链路复用缓存 key）
"""
import json
from io import BytesIO
import time

import pytest

from app.services.ai.mermaid_renderer import ChartCache
from app.services.crypto import encrypt_api_key


# ---------------------------------------------------------------------------
# ChartCache（对应 P0-2 单飞去重 / P0-1 缓存命中，优化均以此不变量为基座）
# ---------------------------------------------------------------------------

class TestChartCache:

    def test_same_payload_same_key_roundtrip(self, tmp_path):
        cache = ChartCache(cache_dir=str(tmp_path))
        code = "graph TD\n    A-->B"
        cache.set(code, b"png-bytes", fmt="png", chart_type="flowchart")
        hit = cache.get(code, fmt="png", chart_type="flowchart")
        assert hit is not None and hit.exists()
        assert hit.read_bytes() == b"png-bytes"

    def test_cache_key_isolates_chart_type(self, tmp_path):
        """同 payload 不同 chart_type 不得互踩缓存（原 BUG 回归保护）"""
        cache = ChartCache(cache_dir=str(tmp_path))
        code = "graph TD\n    A-->B"
        cache.set(code, b"flow", fmt="png", chart_type="flowchart")
        assert cache.get(code, fmt="png", chart_type="gantt") is None
        assert cache.get(code, fmt="png", chart_type="flowchart") is not None

    def test_allow_pil_participates_in_key(self, monkeypatch, tmp_path):
        """get_or_render 的 key 必须包含 allow_pil，否则 PIL 兜底图污染不降级轨"""
        import app.services.ai.mermaid_renderer as mr
        calls = {"n": 0}

        def fake_render(mermaid_code, chart_type, *, skip_http=False, allow_pil=True):
            calls["n"] += 1
            return BytesIO(b"PNG" * 40)

        monkeypatch.setattr(mr, "render_mermaid_to_bytes", fake_render)
        cache = ChartCache(cache_dir=str(tmp_path))
        code = "graph TD\n    A-->B"
        cache.get_or_render(code, "flowchart")                  # allow_pil=True → 渲染第 1 次
        cache.get_or_render(code, "flowchart")                  # 命中 pil=1 缓存 → 不渲染
        cache.get_or_render(code, "flowchart", allow_pil=False)  # pil=0 key 未命中 → 渲染第 2 次
        assert calls["n"] == 2, "allow_pil 必须参与缓存 key，两档互不污染"
        st = cache.get_stats()
        assert st["hits"] == 1 and st["misses"] == 2

    def test_get_or_render_renders_once_then_hits(self, monkeypatch, tmp_path):
        """相同 key 连续调用只渲染一次，第二次命中缓存（并发单飞的缓存层契约）"""
        import app.services.ai.mermaid_renderer as mr
        calls = {"n": 0}

        def fake_render(mermaid_code, chart_type, *, skip_http=False, allow_pil=True):
            calls["n"] += 1
            return BytesIO(b"PNG" * 40)

        monkeypatch.setattr(mr, "render_mermaid_to_bytes", fake_render)
        cache = ChartCache(cache_dir=str(tmp_path))
        code = "graph TD\n    A-->B"
        b1 = cache.get_or_render(code, "flowchart")
        b2 = cache.get_or_render(code, "flowchart")
        assert calls["n"] == 1, "相同 key 应只渲染一次"
        assert b1 is not None and b2 is not None
        assert b1.getvalue() == b2.getvalue()
        st = cache.get_stats()
        assert st["misses"] == 1 and st["hits"] == 1

    def test_concurrent_same_key_renders_once(self, monkeypatch, tmp_path):
        """并发相同 key（P0-2 单飞去重）：4 线程同时渲染只触发 1 次渲染"""
        import threading
        import app.services.ai.mermaid_renderer as mr
        calls = {"n": 0}
        calls_lock = threading.Lock()
        start_gate = threading.Barrier(4)

        def fake_render(mermaid_code, chart_type, *, skip_http=False, allow_pil=True):
            with calls_lock:
                calls["n"] += 1
            import time
            time.sleep(0.15)  # 扩大并发窗口
            return BytesIO(b"PNG" * 40)

        monkeypatch.setattr(mr, "render_mermaid_to_bytes", fake_render)
        cache = ChartCache(cache_dir=str(tmp_path))
        results: list = []
        errors: list = []

        def worker():
            try:
                start_gate.wait()
                b = cache.get_or_render("graph TD\n    A-->B", "flowchart")
                results.append(b.getvalue() if b else None)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert calls["n"] == 1, "并发相同 key 应只渲染一次（thundering herd 防护）"
        assert all(r == b"PNG" * 40 for r in results)
        st = cache.get_stats()
        assert st["misses"] == 1

    def test_lru_eviction_respects_capacity(self, tmp_path):
        """总大小超过上限时必须淘汰最久未访问条目（LRU 容量约束）"""
        cache = ChartCache(cache_dir=str(tmp_path), max_size_mb=0.002)  # ~2KB
        codes = [f"graph TD\n    A{i}-->B{i}" for i in range(12)]
        for c in codes:
            cache.set(c, b"x" * 300, chart_type="flowchart")
        assert cache._stats["total_size"] <= cache._max_size_bytes
        files = list(cache.cache_dir.glob("*.png"))
        assert len(files) < len(codes), "超出容量必须发生 LRU 淘汰"
        # 最近写入的条目一定在；磁盘文件总大小与统计 total_size 守恒
        assert cache.get(codes[-1], chart_type="flowchart") is not None
        disk = sum(f.stat().st_size for f in files)
        assert disk == cache._stats["total_size"]

    def test_clear_resets_state(self, tmp_path):
        cache = ChartCache(cache_dir=str(tmp_path))
        for i in range(3):
            cache.set(f"code-{i}", b"y" * 20, chart_type="flowchart")
        cache.clear()
        st = cache.get_stats()
        assert st["total_size"] == 0
        assert st["file_count"] == 0
        assert cache._access_order == {}


# ---------------------------------------------------------------------------
# provider_factory._fallback_chain（对应 P0-4：加缓存后语义必须不变）
# ---------------------------------------------------------------------------

class TestFallbackChainOrdering:

    @pytest.mark.asyncio
    async def test_chain_respects_priority_and_excludes_primary(self, db_conn):
        import app.services.ai.provider_factory as pf
        rows = [
            ("cfg-c", "zhipu", 0, encrypt_api_key("key-c"), "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus", "2026-09-01T00:00:00"),
            ("cfg-a", "deepseek", 1, encrypt_api_key("key-a"), "https://api.deepseek.com/v1", "deepseek-chat", "2026-09-02T00:00:00"),
            ("cfg-b", "qwen", 2, encrypt_api_key("key-b"), "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max", "2026-09-03T00:00:00"),
        ]
        for rid, pname, prio, key, base, model, ts in rows:
            await db_conn.execute(
                "INSERT INTO ai_config (id, provider_name, is_active, priority, api_key_encrypted, base_url, model, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rid, pname, 1, prio, key, base, model, ts))
        await db_conn.commit()

        chain = await pf._fallback_chain()

        # 主配置 = updated_at 最新（cfg-b）→ 排除；其余按 priority 升序；
        # 若 settings.agnes_api_key 配置了 agnes 兜底，它固定追加在链尾
        names = [c["provider_name"] for c in chain]
        assert "qwen" not in names, "主配置（updated_at 最新）不应出现在降级链"
        assert names[:2] == ["zhipu", "deepseek"]
        assert chain[0]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"

    @pytest.mark.asyncio
    async def test_chain_skips_empty_key_rows(self, db_conn):
        import app.services.ai.provider_factory as pf
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, is_active, priority, api_key_encrypted, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("cfg-no-key", "deepseek", 1, 1, "", "2026-09-01T00:00:00"))
        await db_conn.commit()
        chain = await pf._fallback_chain()
        assert all(c["api_key"] for c in chain), "空 key 配置必须被跳过"


# ---------------------------------------------------------------------------
# sse_handlers 纯函数（对应 P1-1 批量写 / 目录树 O(N) 预构建不变量）
# ---------------------------------------------------------------------------

class TestWordBudgetAllocation:

    def _units(self):
        return {
            "u1": {"unit": {"id": "u1", "title": "单元1"}, "leaves": [
                {"id": "l1"}, {"id": "l2"}, {"id": "l3"}]},
        }

    def test_adopts_ai_allocation_when_sum_matches(self):
        from app.routers.sse_handlers import _apply_word_budget_allocations
        units = self._units()
        _apply_word_budget_allocations(units, 100, {"l1": 40, "l2": 30, "l3": 30})
        assert units["u1"]["alloc"] == {"l1": 40, "l2": 30, "l3": 30}

    def test_falls_back_to_equal_split_with_conservation(self):
        from app.routers.sse_handlers import _apply_word_budget_allocations
        units = self._units()
        # AI 分配缺失 l3 → 总和 91 ≠ 100 → 降级均分且守恒
        _apply_word_budget_allocations(units, 100, {"l1": 90, "l2": 1})
        alloc = units["u1"]["alloc"]
        assert sum(alloc.values()) == 100
        assert set(alloc.keys()) == {"l1", "l2", "l3"}

    def test_single_leaf_unit_skipped(self):
        from app.routers.sse_handlers import _apply_word_budget_allocations
        units = {"u1": {"unit": {"id": "u1"}, "leaves": [{"id": "l1"}]}}
        _apply_word_budget_allocations(units, 100, {"l1": 1})
        assert "alloc" not in units["u1"], "单叶子单元不应被 AI 分配覆盖"


class TestBuildParentChain:

    def _sections(self):
        return [
            {"id": "r1", "parent_id": "", "title": "第一章 总则", "level": 1,
             "sort_order": 0, "outline_json": json.dumps({"id": "1"})},
            {"id": "r2", "parent_id": "", "title": "第二章 施工部署", "level": 1,
             "sort_order": 1, "outline_json": json.dumps({"id": "2"})},
            {"id": "c1", "parent_id": "r1", "title": "1.1 概述", "level": 2,
             "sort_order": 0, "outline_json": json.dumps({"id": "1.1"})},
            {"id": "leaf", "parent_id": "c1", "title": "细节", "level": 3,
             "sort_order": 0, "outline_json": json.dumps({"id": "1.1.1"})},
        ]

    def test_prebuilt_index_path_matches_rebuild(self):
        """预构建 nodes/children_by_parent 与每次重建结果必须一致（防 O(N²) 回归）"""
        from app.routers.sse_handlers import _build_parent_chain
        sections = self._sections()
        nodes = {s["id"]: s for s in sections}
        children: dict[str, list] = {}
        for s in sections:
            children.setdefault(s.get("parent_id", ""), []).append(s)
        for k in children:
            children[k].sort(key=lambda x: x.get("sort_order", 0))

        direct = _build_parent_chain(sections, "leaf", nodes, children)
        rebuilt = _build_parent_chain(sections, "leaf")
        # 标题保留原前缀（"第一章 总则"），编号标签按 outline 规则生成（"第1章"）
        # ✅ 口径更新（2026-09-22 编号命名空间统一）：二级祖先标签由完整点分路径
        #    "1.1" 改为折算显示 "1"（与前端/导出的 一级"第X章"/二级"N"/三级"N.M" 同源）。
        assert direct == rebuilt == "第一章 第一章 总则 > 1 1.1 概述"

    def test_chapter_index_counts_siblings(self):
        from app.routers.sse_handlers import _build_parent_chain
        sections = self._sections()
        nodes = {s["id"]: s for s in sections}
        children: dict[str, list] = {}
        for s in sections:
            children.setdefault(s.get("parent_id", ""), []).append(s)
        chain = _build_parent_chain(sections, "c1", nodes, children)
        assert chain == "第一章 第一章 总则"


# ---------------------------------------------------------------------------
# export 纯函数（对应 P0-3 导出链路 / 渲染轨匹配 key，优化均基于这些解析结果）
# ---------------------------------------------------------------------------

class TestExportPureFunctions:

    def test_norm_code_folds_whitespace(self):
        from app.routers.export import _norm_code
        assert _norm_code("graph TD\n    A-->B") == _norm_code("graph TD  A-->B")
        assert _norm_code("  a  b ") == "a b"

    def test_detect_plain_heading(self):
        from app.routers.export import _detect_plain_heading
        assert _detect_plain_heading("2 法律法规依据") == (2, "法律法规依据")
        assert _detect_plain_heading("3.1 国家标准") == (3, "国家标准")
        assert _detect_plain_heading("**1 施工条件分析**") == (2, "施工条件分析")
        # 年份/数量词不得误判为标题
        assert _detect_plain_heading("2023 年完成主体结构验收。") is None
        assert _detect_plain_heading("100 人团队进场") is None
        # 正文句末标点排除
        assert _detect_plain_heading("2 本方案适用于本工程。") is None
        # ✅ 2026-09-19：全角分号/逗号收尾的列举条目不得误判为标题
        assert _detect_plain_heading("3.1.12 身份证复印件、照片；") is None
        assert _detect_plain_heading("3.1 搭设完成并验收合格，方可投入使用，") is None

    def test_cn_numeral_to_int(self):
        from app.routers.export import _cn_numeral_to_int
        assert _cn_numeral_to_int("二十一") == "21"
        assert _cn_numeral_to_int("二十三") == "23"
        assert _cn_numeral_to_int("一百零五") == "105"
        assert _cn_numeral_to_int("九十九") == "99"
        assert _cn_numeral_to_int("12") == "12"
        assert _cn_numeral_to_int("混合") == "混合"  # 解析失败返回原串

    def test_section_number_prefix(self):
        from app.routers.export import _section_number_prefix
        assert _section_number_prefix("第一章 施工组织设计") == "1"
        assert _section_number_prefix("1.2 进度计划") == "1.2"
        assert _section_number_prefix("（一） 设计标准") == ""

    def test_strip_title_number(self):
        from app.routers.export import _strip_title_number
        assert _strip_title_number("第一章 第一章 工程概况") == "工程概况"
        assert _strip_title_number("1.1 项目基本信息") == "项目基本信息"

    def test_parse_content_blocks_full_markdown(self):
        from app.routers.export import _parse_content_blocks
        md = (
            "# 1 总体概述\n\n"
            "正文段落。\n\n"
            "| 序号 | 名称 |\n"
            "| --- | --- |\n"
            "| 1 | 基坑 |\n\n"
            "```mermaid\n"
            "graph TD\n    A-->B\n"
            "```\n\n"
            "[CHART_TYPE: gantt]\n\n"
            "- 无序项\n"
            "2. 有序项\n"
        )
        blocks = _parse_content_blocks(md)
        types = [b["type"] for b in blocks]
        assert types == ["heading", "paragraph", "table", "chart", "chart", "list_item", "list_item"]
        assert blocks[3]["chart_type"] == "flowchart" and blocks[3]["inline"] is True
        assert blocks[4]["chart_type"] == "gantt"
        assert blocks[5]["ordered"] is False
        assert blocks[6]["ordered"] is True

    def test_parse_content_blocks_unclosed_code_fence_truncated(self):
        from app.routers.export import _parse_content_blocks
        md = "```mermaid\ngraph TD\n    A-->B\n"  # 未闭合围栏
        blocks = _parse_content_blocks(md)
        # ✅ 行为变更（2026-09-19）：未闭合围栏 = 生成被截断（实测交付文档取证，
        #    `flowchart LR` 只写到 `B --> C{`）。旧断言要求仍产出 chart 块，
        #    但残片渲染必然失败（红字占位）；新口径与登记侧
        #    `_scan_inline_charts` 的"未闭合块不提取"一致 —— 整块跳过。
        #    不变式不变的部分：**不得吞噬正文**（此处只验证无块产出，见下方正文守护）。
        assert [b for b in blocks if b["type"] in ("chart", "code")] == []

    def test_unclosed_fence_does_not_swallow_following_text(self):
        """未闭合围栏之外的正文不得被吞（截断保护的原始意图）。"""
        from app.routers.export import _parse_content_blocks
        md = "```mermaid\ngraph TD\n    A-->B\n\n后续正文段落。\n"
        blocks = _parse_content_blocks(md)
        paras = [b for b in blocks if b["type"] == "paragraph"]
        assert any("后续正文段落" in (b.get("text") or "") for b in paras), blocks


# ---------------------------------------------------------------------------
# export 倒排索引（对应 P0-3：O(N²) 兜底扫描 → O(1)，语义必须与旧实现完全等价）
# ---------------------------------------------------------------------------

class TestChartTypeIndex:

    def test_fallback_keeps_first_match_semantics(self):
        from app.routers.export import _build_chart_type_index, _find_fallback_code
        lookup = {
            ("s1", "flowchart"): "graph TD\n    A-->B",
            ("s2", "gantt"): "gantt\n    title x",
            ("s3", "flowchart"): "graph TD\n    C-->D",
            ("s4", "flowchart"): "",  # 空 code 不入索引，也不参与兜底
        }
        idx = _build_chart_type_index(lookup)
        # 与旧 for-break 语义一致：取该类型第一个非空 code
        assert _find_fallback_code(idx, "flowchart") == "graph TD\n    A-->B"
        assert _find_fallback_code(idx, "gantt") == "gantt\n    title x"
        assert _find_fallback_code(idx, "timeline") == ""
        assert _find_fallback_code(idx, "flowchart") == next(
            cd for (_sid, c), cd in lookup.items() if c == "flowchart" and cd)

    def test_index_equivalent_to_old_linear_scan(self):
        """倒排索引与旧全表扫描在所有输入下结果一致（等价替换回归保护）"""
        import random
        from app.routers.export import _build_chart_type_index, _find_fallback_code
        types = ["flowchart", "gantt", "architecture", "labor",
                 "comparison", "layout", "timeline"]
        rng = random.Random(42)
        lookup: dict[tuple[str, str], str] = {}
        for i in range(30):
            ct = rng.choice(types)
            code = f"graph TD\n    A{i}-->B{i}" if rng.random() > 0.2 else ""
            lookup[(f"s{i}", ct)] = code

        def old_scan(chart_lookup, chart_type):
            for (_sid, c), cd in chart_lookup.items():
                if c == chart_type and cd:
                    return cd
            return ""

        idx = _build_chart_type_index(lookup)
        for ct in types:
            assert _find_fallback_code(idx, ct) == old_scan(lookup, ct)

    def test_index_ignores_empty_codes(self):
        from app.routers.export import _build_chart_type_index
        idx = _build_chart_type_index({("s1", "flowchart"): "", ("s2", "flowchart"): None})
        assert "flowchart" not in idx


# ---------------------------------------------------------------------------
# provider_factory 缓存与审计攒批（对应 P0-4）
# ---------------------------------------------------------------------------

class TestFallbackChainCaching:

    @pytest.mark.asyncio
    async def test_chain_cached_within_ttl(self, db_conn):
        """TTL 内命中缓存，invalidate_config_cache 后强制重读"""
        import app.services.ai.provider_factory as pf
        pf._fallback_cache["data"] = None
        pf._fallback_cache["ts"] = 0.0
        # 两条配置：cfg-primary（updated_at 最新=主配置，被排除）、cfg-backup（备选，入链）
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, is_active, priority, api_key_encrypted, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("cfg-backup", "deepseek", 1, 1, encrypt_api_key("key-1"), "2026-09-01T00:00:00"))
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, is_active, priority, api_key_encrypted, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("cfg-primary", "qwen", 1, 2, encrypt_api_key("key-2"), "2026-09-02T00:00:00"))
        await db_conn.commit()

        chain1 = await pf._fallback_chain()
        assert [c["provider_name"] for c in chain1][:1] == ["deepseek"], "主配置排除、备选入链"

        # TTL 内修改 DB（未失效缓存）→ 第二次调用仍返回缓存
        await db_conn.execute("DELETE FROM ai_config")
        await db_conn.commit()
        chain2 = await pf._fallback_chain()
        assert chain2 == chain1, "TTL 内应命中缓存"

        # 失效后强制重读 → 感知 DB 变更（链中不再有 deepseek）
        pf.invalidate_config_cache()
        chain3 = await pf._fallback_chain()
        assert all(c["provider_name"] != "deepseek" for c in chain3)


class TestAuditBatching:

    @pytest.mark.asyncio
    async def test_periodic_loop_flushes_single_row_without_next_call(self, db_conn, monkeypatch):
        """单条审计后即使没有下一次调用，周期任务也必须按时落库。"""
        import asyncio
        import app.services.ai.provider_factory as pf
        pf._audit_buffer.clear()
        monkeypatch.setattr(pf, "_AUDIT_FLUSH_INTERVAL", 0.01)
        pf._audit_buffer.append((
            "periodic-one", "deepseek", "deepseek-chat", "chat",
            1, 1, 0, 0.1, 1, "", "content_draft"))
        stop = asyncio.Event()
        task = asyncio.create_task(pf.audit_flush_loop(stop))
        found = 0
        try:
            for _ in range(20):
                await asyncio.sleep(0.01)
                cur = await db_conn.execute(
                    "SELECT COUNT(*) FROM ai_audit_logs WHERE id='periodic-one'")
                found = (await cur.fetchone())[0]
                if found == 1:
                    break
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert found == 1

    @pytest.mark.asyncio
    async def test_audit_batches_until_threshold(self, db_conn):
        """审计攒批：未达阈值不落库；满 50 条触发一次批量落库；flush 兜底"""
        import app.services.ai.provider_factory as pf
        pf._audit_buffer.clear()
        pf._audit_last_flush["ts"] = time.time()  # 重置 10s 兜底计时，避免整会话时长误触发 flush
        try:
            for _ in range(3):
                await pf._log_audit("deepseek", "deepseek-chat", "chat", 1.2, True)
            cur = await db_conn.execute("SELECT COUNT(*) AS c FROM ai_audit_logs")
            assert (await cur.fetchone())["c"] == 0, "未达阈值不应落库"

            await pf._flush_audit_buffer()
            cur = await db_conn.execute("SELECT COUNT(*) AS c FROM ai_audit_logs")
            assert (await cur.fetchone())["c"] == 3, "flush 应落库缓冲内全部"

            for _ in range(47):
                await pf._log_audit("deepseek", "deepseek-chat", "chat", 0.5, True)
            for _ in range(3):  # 第 50 条触发批量落库
                await pf._log_audit("deepseek", "deepseek-chat", "chat", 0.5, True)
            cur = await db_conn.execute("SELECT COUNT(*) AS c FROM ai_audit_logs")
            assert (await cur.fetchone())["c"] == 53, "满批次应自动落库"
        finally:
            pf._audit_buffer.clear()
