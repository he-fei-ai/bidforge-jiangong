"""R210（2026-11-10）· 目录生成模块遗留项收口 —— 新增护栏。

本轮处理 R209 报告 §9 登记的 10 项遗留（用户指令：「剩余未修项遗留问题与后续建议
按最优方案执行修复和加固处理」）：

落地 4 项：

- **L1-7（P1）断点续跑「陈旧复用」**：R209 让续跑真正生效后，用户中断后改了
  项目资料 / 提取了新事实再重新生成，被复用的章仍是**旧快照** ⇒ 目录与最新资料
  不一致且**用户零感知**。现加**输入指纹**（资料/事实/编制依据/施工内容清单的
  sha256 前 16 位）：指纹不一致 ⇒ 不续跑（全量重算）。
- **L1-8（P3）断点续跑「无前端可见信号」**：后端日志早有「复用 N 章」，但 SSE 事件
  与前端提示卡零信号 ⇒ 用户不知道这次省了多少配额。现加法式回传 `resumed_chapters`。
- **L1-2（P3）分步生成 L1 一级章数无上限**：AI 幻觉出几百个一级章 → 逐章 AI 调用
  成本爆炸。现按上限截断（fail-soft）+ 回传 `level1_truncated` / `level1_total`。
- **L1-3（P3）读路径看不到「不可达节点」**：`parent_id` 成环/悬挂的章节在树里
  整体不可达，只有结构自检与严格导出守卫会提示 ⇒ 现加法式回传 `unmounted_count`。

证伪 + 加固 3 项（用护栏钉住「正确语义」，防止被当缺陷误改）：

- **L1-4** `review_records` 删章不清理 = **刻意留痕**（评审轨迹可回溯），非孤儿残留；
- **L1-9** `heading_v1` 非死代码（`heading_standard` 兼容再导出），但**导出链不得**
  回退到 V1（编号口径唯一 = V2）；
- **L1-10** 导出/树/正文三处排序键已由 R102 收敛到 `section_sort_order_key` 单一出口。
"""

import json
from pathlib import Path

import pytest

SCHEME = "s-r210"
PROJECT = "p-r210"


# ---------------------------------------------------------------------------
# A 组 · L1-7 输入指纹（断点续跑防陈旧复用）
# ---------------------------------------------------------------------------


class TestOutlineResumeFingerprintR210:
    """L1-7：续跑来源的「输入指纹」判据。"""

    def test_fingerprint_is_deterministic_and_field_separated(self):
        """指纹确定性 + 字段边界不歧义（"ab"+"c" ≠ "a"+"bc"）。"""
        from app.routers.sse_handlers import _outline_input_fingerprint

        assert _outline_input_fingerprint("a", "b") == _outline_input_fingerprint("a", "b")
        assert _outline_input_fingerprint("a", "b") != _outline_input_fingerprint("b", "a")
        assert _outline_input_fingerprint("ab", "c") != _outline_input_fingerprint("a", "bc")
        assert len(_outline_input_fingerprint("x")) == 16

    def test_fingerprint_is_not_in_frontend_whitelist(self):
        """指纹是**内部判据**，不进前端白名单（不得外泄到浏览器）。"""
        from app.services import checkpoint_schema as cs

        assert "input_fingerprint" not in cs.CHECKPOINT_KINDS["outline_generation"][1]

    @pytest.mark.asyncio
    async def test_checkpoint_partial_outline_carries_fingerprint(self, db_conn):
        """部分成果 checkpoint 必须带上指纹（下次续跑据此判「是否陈旧」）。"""
        from app.routers.sse_checkpoint import _checkpoint_partial_outline, _load_outline_checkpoint
        from app.services.ai.task_registry import finish_task, register_task

        tid = await register_task("outline_generation", "p", "s")
        assert await _checkpoint_partial_outline(tid, [{"title": "第1章"}], [], input_fingerprint="FP123") is True
        ckpt = await _load_outline_checkpoint(tid)
        assert ckpt["input_fingerprint"] == "FP123"
        await finish_task(tid, "stopped")

    @pytest.mark.asyncio
    async def test_checkpoint_partial_outline_without_fingerprint_is_unchanged(self, db_conn):
        """不传指纹时载荷逐字不变（向后兼容）。"""
        from app.routers.sse_checkpoint import _checkpoint_partial_outline, _load_outline_checkpoint
        from app.services.ai.task_registry import finish_task, register_task

        tid = await register_task("outline_generation", "p", "s")
        assert await _checkpoint_partial_outline(tid, [{"title": "第1章"}], []) is True
        ckpt = await _load_outline_checkpoint(tid)
        assert "input_fingerprint" not in ckpt
        await finish_task(tid, "stopped")


# ---------------------------------------------------------------------------
# B 组 · 端到端：续跑 / 截断 / 可见信号
# ---------------------------------------------------------------------------


def _mk_outline_harness(
    monkeypatch,
    sh,
    *,
    total,
    resumed_titles=(),
    fingerprint="",
    prev_fingerprint="__SAME__",
    window=2,
    merge_k=1,
):
    """装配长方案分步生成的桩，返回 (被调用章序号列表, 运行协程)。

    ``fingerprint`` = 本次运行的输入指纹（monkeypatch `_outline_input_fingerprint`）；
    ``prev_fingerprint`` = 上次 checkpoint 里存的指纹（``"__SAME__"`` = 与本次相同；
    ``None`` = 不写该键 ⇒ 旧数据形态）。
    """
    import uuid

    monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
    monkeypatch.setattr(sh, "OUTLINE_CHAPTER_CONCURRENCY", window)
    monkeypatch.setattr(sh, "OUTLINE_CHAPTER_BATCH_SIZE", merge_k)
    if fingerprint:
        monkeypatch.setattr(sh, "_outline_input_fingerprint", lambda *parts: fingerprint)

    calls: list[int] = []

    async def fake_fetch_unit(unit_chapters, *, batch_prompt=None, single_prompts=None,
                              task_id=None, sem=None, timeout=60, push_stats=None, **kw):
        calls.extend(i for i, _ in unit_chapters)
        per = [("ok", [{"title": f"第{i + 1}章-小节{i + 1}.1", "children": []}]) for i, _ in unit_chapters]
        return "ok", per

    async def fake_collect(messages, validate_fn=None, **kwargs):
        outline = [{"title": f"第{i}章", "description": "", "children": []} for i in range(1, total + 1)]
        return ({"outline": outline}, json.dumps({"outline": outline}, ensure_ascii=False))

    async def fake_review(outline, *a, **kw):
        return outline, {}

    monkeypatch.setattr(sh, "_fetch_unit_children", fake_fetch_unit)
    monkeypatch.setattr(sh, "collect_json_response", fake_collect)
    monkeypatch.setattr(sh, "_review_and_fix_outline", fake_review)

    async def _insert_task(conn, tid, scheme_id, status, checkpoint, updated_at="2026-11-10T00:00:00"):
        await conn.execute(
            "INSERT OR REPLACE INTO task_registry "
            "(id, task_type, project_id, scheme_id, status, progress, checkpoint_json, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, "outline_generation", "p-r210", scheme_id, status, 0.5,
             json.dumps(checkpoint, ensure_ascii=False), updated_at),
        )
        await conn.commit()

    async def _run(conn):
        sid, pid = uuid.uuid4().hex, uuid.uuid4().hex
        await conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "R210 续跑验证"))
        await conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name, word_budget, config_json) VALUES (?,?,?,?,?)",
            (sid, pid, "R210 断点续跑验证", 10000, json.dumps({}, ensure_ascii=False)),
        )
        await conn.commit()
        if resumed_titles:
            ckpt = {
                "kind": "outline_result",
                "event": "stopped",
                "partial": True,
                "outline": [
                    {"title": t, "children": [{"title": f"{t}-小节", "children": []}]} for t in resumed_titles
                ],
            }
            _prev_fp = fingerprint if prev_fingerprint == "__SAME__" else prev_fingerprint
            if _prev_fp:
                ckpt["input_fingerprint"] = _prev_fp
            await _insert_task(conn, "t-old-r210", sid, "stopped", ckpt)

        resp = await sh.generate_outline(sid, None, conn)
        events: list[dict] = []
        async for chunk in resp.body_iterator:
            for line in (chunk if isinstance(chunk, str) else chunk.decode("utf-8")).splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
        return events

    return calls, _run


class TestResumeFingerprintBehaviourR210:
    """L1-7 端到端：指纹不一致 ⇒ 不续跑。"""

    @pytest.mark.asyncio
    async def test_fingerprint_mismatch_blocks_resume(self, db_conn, monkeypatch):
        """上次成果的指纹与本次不同 ⇒ 旧章是过时快照 ⇒ **不得复用**（全量重算）。"""
        from app.routers import sse_handlers as sh

        calls, run = _mk_outline_harness(
            monkeypatch,
            sh,
            total=4,
            resumed_titles=["第1章", "第3章"],
            fingerprint="FP-NEW",
            prev_fingerprint="FP-OLD",
        )
        events = await run(db_conn)
        assert calls == [0, 1, 2, 3], f"指纹不一致时必须全量重算，实际 {calls}"
        last = events[-1]
        assert "resumed_chapters" not in last, last
        titles = [n.get("title") for n in last["outline"]]
        assert titles == ["第1章", "第2章", "第3章", "第4章"], titles

    @pytest.mark.asyncio
    async def test_legacy_checkpoint_without_fingerprint_still_resumes(self, db_conn, monkeypatch):
        """旧 checkpoint（无指纹键）⇒ 不比较、照常续跑（向后兼容）。"""
        from app.routers import sse_handlers as sh

        calls, run = _mk_outline_harness(
            monkeypatch,
            sh,
            total=4,
            resumed_titles=["第1章", "第3章"],
            fingerprint="FP-NEW",
            prev_fingerprint=None,  # 旧数据：checkpoint 里没有该键
        )
        events = await run(db_conn)
        assert calls == [1, 3], f"旧 checkpoint 不得阻断续跑，实际 {calls}"
        assert events[-1].get("resumed_chapters") == 2, events[-1]

    @pytest.mark.asyncio
    async def test_resume_runs_when_fingerprint_matches(self, db_conn, monkeypatch):
        """指纹一致 ⇒ 照常续跑（复用章跳过 AI 调用）。"""
        from app.routers import sse_handlers as sh

        calls, run = _mk_outline_harness(
            monkeypatch, sh, total=4, resumed_titles=["第1章", "第3章"], fingerprint="FP-SAME"
        )
        events = await run(db_conn)
        assert calls == [1, 3], f"指纹一致时必须复用（只调未续跑章），实际 {calls}"
        last = events[-1]
        assert last.get("resumed_chapters") == 2, last
        titles = [n.get("title") for n in last["outline"]]
        assert titles == ["第1章", "第2章", "第3章", "第4章"], titles


class TestResumeVisibilityR210:
    """L1-8：复用章数对前端可见（加法式）。"""

    @pytest.mark.asyncio
    async def test_no_resume_means_key_absent(self, db_conn, monkeypatch):
        """未复用时**不出现** `resumed_chapters`（既有响应逐字节不变）。"""
        from app.routers import sse_handlers as sh

        calls, run = _mk_outline_harness(monkeypatch, sh, total=3, resumed_titles=[])
        events = await run(db_conn)
        last = events[-1]
        assert "resumed_chapters" not in last, last
        assert calls == [0, 1, 2], calls

    def test_whitelist_and_generated_ts_cover_resumed_chapters(self):
        """白名单单一事实源已登记 + 前端 TS 已重新生成（否则断线重挂读不到）。"""
        from pathlib import Path

        from app.services import checkpoint_schema as cs

        assert "resumed_chapters" in cs.CHECKPOINT_KINDS["outline_generation"][1]
        assert cs.CHECKPOINT_FIELD_TYPES["outline_result"]["resumed_chapters"] == "int"
        ts = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "checkpointSchema.ts"
        assert "resumed_chapters?: number;" in ts.read_text(encoding="utf-8")


class TestLevel1CapR210:
    """L1-2：一级章数上限（fail-soft 截断 + 可见信号）。"""

    @pytest.mark.asyncio
    async def test_level1_over_cap_is_truncated_with_signal(self, db_conn, monkeypatch):
        from app.routers import sse_handlers as sh

        monkeypatch.setattr(sh, "OUTLINE_MAX_LEVEL1_CHAPTERS", 2)
        calls, run = _mk_outline_harness(monkeypatch, sh, total=5)
        events = await run(db_conn)
        last = events[-1]
        assert calls == [0, 1], f"超限后只应调度上限内的章，实际 {calls}"
        assert last.get("level1_truncated") == 3, last
        assert last.get("level1_total") == 5, last
        titles = [n.get("title") for n in last["outline"]]
        assert titles == ["第1章", "第2章"], titles

    @pytest.mark.asyncio
    async def test_level1_under_cap_keeps_response_shape(self, db_conn, monkeypatch):
        """未超限 ⇒ 不出现截断键（既有响应逐字节不变）。"""
        from app.routers import sse_handlers as sh

        monkeypatch.setattr(sh, "OUTLINE_MAX_LEVEL1_CHAPTERS", 40)
        calls, run = _mk_outline_harness(monkeypatch, sh, total=3)
        events = await run(db_conn)
        last = events[-1]
        assert "level1_truncated" not in last, last
        assert calls == [0, 1, 2], calls

    @pytest.mark.asyncio
    async def test_cap_zero_means_unlimited(self, db_conn, monkeypatch):
        """上限 = 0 ⇒ 不限（回到旧行为）。"""
        from app.routers import sse_handlers as sh

        monkeypatch.setattr(sh, "OUTLINE_MAX_LEVEL1_CHAPTERS", 0)
        calls, run = _mk_outline_harness(monkeypatch, sh, total=4)
        events = await run(db_conn)
        assert calls == [0, 1, 2, 3], calls
        assert "level1_truncated" not in events[-1], events[-1]


# ---------------------------------------------------------------------------
# C 组 · L1-3 读路径暴露不可达节点
# ---------------------------------------------------------------------------


class TestUnmountedVisibleR210:
    """L1-3：`parent_id` 成环/悬挂的章节在读路径可见。"""

    @pytest.mark.asyncio
    async def test_list_sections_reports_unmounted_count(self, db_conn):
        """有环/悬挂节点时 `unmounted_count` > 0（读路径不再零信号）。"""
        from app.routers.sections import list_sections

        await db_conn.execute("INSERT OR IGNORE INTO projects (id,name) VALUES (?,?)", (PROJECT, "R210"))
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id,project_id,name) VALUES (?,?,?)",
            (SCHEME, PROJECT, "R210 不可达节点验证"),
        )
        # a → b → a（成环）：两个节点都不是根 ⇒ 树里不可达
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order)"
            " VALUES (?,?,?,?,?,?,?)",
            ("sec-a", SCHEME, PROJECT, "sec-b", "环内甲", 2, 0),
        )
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order)"
            " VALUES (?,?,?,?,?,?,?)",
            ("sec-b", SCHEME, PROJECT, "sec-a", "环内乙", 2, 1),
        )
        # 悬挂：parent_id 指向不存在的章节
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level, sort_order)"
            " VALUES (?,?,?,?,?,?,?)",
            ("sec-c", SCHEME, PROJECT, "sec-ghost", "悬挂章", 2, 2),
        )
        await db_conn.commit()

        res = await list_sections(SCHEME, True, db_conn)
        assert res.get("unmounted_count") == 2, res.get("unmounted_count")
        # 正常节点仍可见（不阻断）
        assert isinstance(res.get("tree"), list)

    def test_unmounted_check_reuses_shared_id_collector(self):
        """读路径自检**必须复用**模块级 `_collect_section_ids`（不得内联第三份等价实现）。

        背景：`sections.py` 已有模块级 `_collect_section_ids`（递归收集树中 id），
        另有两处嵌套 `_collect_mounted` 做同口径的事。本轮初版在 `list_sections`
        里**又内联**一份 `_collect` ⇒ 同一功能四份实现、强度逐份漂移，且新增了
        一个未登记的遍历点（被建树点棘轮锁当场拦下）。
        """
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "sections.py").read_text(
            encoding="utf-8", errors="replace"
        )
        assert "_collect_section_ids(tree)" in src, "读路径未复用模块级 id 收集器"
        assert "def _collect(" not in src, "sections.py 出现内联 _collect ⇒ 同族实现又增一份"


# ---------------------------------------------------------------------------
# E 组 · 停止态 checkpoint 成果完整性（R210 回归护栏）
# ---------------------------------------------------------------------------


class TestStoppedCheckpointIntegrityR210:
    """R210 回归：`_ckpt_payload` 拷贝早于成果字段写入 ⇒ checkpoint 丢成果。

    初版把 `dict(payload)` 放在 `payload["outline"] = outline` **之前** ⇒
    落库的 checkpoint 里没有 outline/failed_count ⇒ 断线重挂读到的成果为空
    （既有护栏 `test_outline_stopped_recovery` 当场抓红）。
    """

    @pytest.mark.asyncio
    async def test_stopped_checkpoint_carries_outline_and_fingerprint(self, db_conn):
        """停止态 checkpoint 必须同时带 outline / failed_count / input_fingerprint。"""
        import app.routers.sse_handlers as sh

        await db_conn.execute("INSERT OR IGNORE INTO projects (id,name) VALUES (?,?)", (PROJECT, "R210"))
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id,project_id,name) VALUES (?,?,?)",
            (SCHEME, PROJECT, "R210 停止态成果"),
        )
        await db_conn.execute(
            "INSERT INTO task_registry (id, task_type, project_id, scheme_id, status, progress, checkpoint_json)"
            " VALUES (?,?,?,?,?,?,?)",
            ("t-r210-stop", "outline_generation", PROJECT, SCHEME, "running", 0.0, "{}"),
        )
        await db_conn.commit()

        outline = [{"title": "第一章 工程概况", "children": []}]
        holder = {
            "outline": outline,
            "failed_chapters": ["第二章"],
            "resumed_chapters": 3,
            "input_fingerprint": "FP-R210",
        }
        await sh._finish_stopped_with_partial("t-r210-stop", holder)

        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id=?", ("t-r210-stop",)
        )
        row = await cur.fetchone()
        ckpt = json.loads(row["checkpoint_json"])
        assert ckpt.get("outline") == outline, f"停止态 checkpoint 丢了成果：{sorted(ckpt)}"
        assert ckpt.get("failed_count") == 1
        assert ckpt.get("resumed_chapters") == 3
        assert ckpt.get("input_fingerprint") == "FP-R210"

    def test_fingerprint_is_not_in_sse_event_payload(self):
        """指纹是内部判据：**不得**出现在 SSE 事件载荷里（只进 checkpoint）。"""
        from app.routers import _sse_outline_review as sor

        src = Path(sor.__file__).read_text(encoding="utf-8", errors="replace")
        # 指纹只在 _ckpt_payload 分支写入，不得直接写进 payload（SSE 载荷）
        # 注意：`_ckpt_payload[...]` 本身含子串 `payload[...]`，故按整行前缀精确判定。
        assert "\n    payload[\"input_fingerprint\"]" not in src, "输入指纹泄漏进 SSE 事件载荷"
        assert '_ckpt_payload["input_fingerprint"]' in src


# ---------------------------------------------------------------------------
# D 组 · 证伪项的加固护栏（钉住「正确语义」）
# ---------------------------------------------------------------------------


class TestDeliberateSemanticsR210:
    """证伪 3 项：把「刻意设计」用护栏钉死，防止被当缺陷误改。"""

    def test_review_records_are_never_deleted_by_chapter_writes(self):
        """L1-4：删章 / 清空目录**不得**删 `review_records`（刻意留痕，可回溯评审轨迹）。

        `review.py` 的模块说明写明「每次状态流转落一条 review_records，可回溯完整
        评审轨迹」；章节被删后该轨迹仍带 `section_title` 冗余字段可读 ⇒ 属刻意
        留痕，不是孤儿残留。本护栏锁住这一意图。
        """
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[1] / "app"
        hits = [
            p.name
            for p in app_dir.rglob("*.py")
            if "DELETE FROM review_records" in p.read_text(encoding="utf-8", errors="replace")
        ]
        assert not hits, f"review_records 是刻意留痕（评审轨迹），不得被章节写路径删除：{hits}"

    def test_export_chain_must_not_fall_back_to_heading_v1(self):
        """L1-9：`heading_v1` 仅为兼容再导出，**导出链不得**引用它（编号口径唯一 = V2）。"""
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[1] / "app"
        for rel in ("routers/export.py", "routers/_export_issue_scan.py"):
            src = (app_dir / rel).read_text(encoding="utf-8", errors="replace")
            assert "heading_v1" not in src, f"{rel} 回退到 heading_v1（编号口径将分叉）"

    def test_section_sort_order_key_is_single_exit(self):
        """L1-10：写侧 (sort_order, id) 与导出侧 (sort_order, level, id) 的**等价性**
        依赖「同级 level 恒等」不变式（R86 L6 已锁：renumber 把 level 刷成位置深度
        ⇒ 同父子节点 level 自愈）；且两侧的 sort_order 归一必须走**同一出口**，
        禁止再内联同形键（否则脏值会在某一侧抛 TypeError）。
        """
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[1] / "app"
        sections_src = (app_dir / "routers" / "sections.py").read_text(encoding="utf-8", errors="replace")
        scan_src = (app_dir / "routers" / "_export_issue_scan.py").read_text(encoding="utf-8", errors="replace")
        export_src = (app_dir / "routers" / "export.py").read_text(encoding="utf-8", errors="replace")
        content_src = (app_dir / "services" / "content_utils.py").read_text(encoding="utf-8", errors="replace")
        assert "def section_sort_order_key" in content_src, "单一出口定义缺失"
        assert "section_sort_order_key" in sections_src, "sections 树排序未走单一出口"
        assert "def _section_sort_key" in scan_src, "导出侧排序键函数缺失"
        assert "section_sort_order_key" in scan_src, "导出侧排序键未走单一出口"
        assert "_section_sort_key" in export_src, "导出链未使用统一排序键"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
