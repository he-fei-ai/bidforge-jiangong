"""R105（2026-10-18）· 正文生成「顺序留痕 + 全局事实调用」接线收口护栏。

本轮修复的是**决定性断链**：正文线 R102 交付的四个服务层收口函数
``verify_generation_order`` / ``prev_leaf_tail_summary`` /
``fact_injection_audit`` / ``chapter_fact_mapping``（外加纯汇总
``summarize_fact_mapping``）在 ``sse_handlers.generate_content`` 只有
import、**零调用点**（ruff F401 ×4 实证），且 ``content_runtime`` 的
``prev_leaf_summary`` 形参与 ``config.content_prev_leaf_context`` 开关
均为「配了不生效」形态。本文件既锁**行为**（端到端跑真实 SSE 流断言
终态/checkpoint/落库报告字段），也锁**结构与顺序**（接线位置、单一出口、
三拼装点同键、fail-soft），防止接线再次漂移成 imported-but-unused。

覆盖（A~F）：
  A  G1 顺序留痕：派发序在信号量放行后记录；终态 order_check 与目录 DFS
     期望序一致；三终态拼装点同键；WARNING 一次闸。
  B  G5 前驱叶子承接：第二章提示词含「前一章正文结尾」段、首章不含；
     开关关闭逐字回退；服务层取片段失败 fail-soft。
  C  G3 注入审计：fact_injection_audit 与裸 _render_facts_text 逐字节一致；
     正文链零裸调用（单一出口）；ckpt fact_injection_summary 与落库报告
     fact_audit 键。
  D  G4 映射表：ckpt fact_mapping 全量（与 leaves 同序）；终态
     fact_mapping_stats 汇总口径恒等；summarize 纯函数降级形态。
  E  fail-soft：映射表计算炸 / 顺序校验炸均不阻断生成与终态。
  F  字节体检：CRLF 保持、无 BOM、无 FFFD。
"""

import inspect
import json
from pathlib import Path

import pytest
from app.routers import sse_handlers as sh
from app.services.content_utils import (
    prev_leaf_tail_summary,
    summarize_fact_mapping,
)
from app.services.facts_builder import (
    _filter_facts_rows,
    _render_facts_text,
    chapter_fact_mapping,
    fact_injection_audit,
)

_SH_FILE = (Path(__file__).resolve().parents[1]
            / "app" / "routers" / "sse_handlers.py")
_CU_FILE = (Path(__file__).resolve().parents[1]
            / "app" / "services" / "content_utils.py")


def _src() -> str:
    return inspect.getsource(sh.generate_content)


# ============================================================================
# 端到端 harness（沿用 test_content_terminal_payload_20261005 范式并扩展：
# 捕获 chat messages 与落库报告 JSON）
# ============================================================================

class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _DB:
    """最小桩 DB：合规四级树（R151）；额外捕获含 last_generation_report 的落库参数。"""

    def __init__(self):
        self.saved: list[str] = []
        self.reports: list[str] = []
        self._sections = self.compliant_sections()

    @staticmethod
    def _sec(sid, num, sort, level=1, parent=""):
        return {
            "id": sid, "scheme_id": "sc1", "title": f"支护章节 {sid}",
            "level": level, "parent_id": parent, "sort_order": sort,
            "word_budget": 20, "content": "", "status": "empty",
            "description": "d",
            "outline_json": json.dumps({"id": num}, ensure_ascii=False),
            "generation_standard": "", "review_status": "", "word_count": 0,
        }

    @classmethod
    def compliant_sections(cls, budget=20):
        """✅ R151（2026-10-31）：合规四级树（1×3×3×4 = 36 个四级叶子，恰在下限）
        —— 旧的「两个一级叶子」会被正文前置闸门拒绝（叶子必须是四级）。"""
        rows = [cls._sec("L1", "1", 0)]
        sort = 1
        for j in range(1, 4):
            rows.append(cls._sec(f"L2{j}", f"1.{j}", sort, 2, "L1"))
            sort += 1
            for k in range(1, 4):
                rows.append(cls._sec(f"L3{j}{k}", f"1.{j}.{k}", sort, 3, f"L2{j}"))
                sort += 1
                for m in range(1, 5):
                    rows.append(cls._sec(f"L4{j}{k}{m}", f"1.{j}.{k}.{m}", sort,
                                         4, f"L3{j}{k}"))
                    sort += 1
        return rows

    def leaf_count(self) -> int:
        return sum(1 for s in self._sections if int(s["level"]) >= 4)

    class _Cur:
        def __init__(self, rows):
            self._rows = rows
            # R156 fixture upgrade: emulate real aiosqlite cursor rowcount
            # (content persist goes through safe_rowcount single exit)
            self.rowcount = 1

        async def fetchone(self):
            return self._rows[0] if self._rows else None

        async def fetchall(self):
            return list(self._rows)

    async def execute(self, sql, params=()):
        s = str(sql)
        if "FROM schemes" in s:
            return self._Cur([{
                "id": "sc1", "project_id": "p1", "name": "测试方案",
                "type": "深基坑", "word_budget": 3000, "config_json": "{}",
                "auto_consistency_repair": 0, "generation_standard": "",
            }])
        if "FROM sections" in s and "ORDER BY sort_order" in s:
            return self._Cur([dict(x) for x in self._sections])
        if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
            self.saved.append(params[0])
            # 报告与正文同一条 UPDATE（last_generation_report=?），扫参数取 JSON
            for p in params:
                if isinstance(p, str) and '"fact_audit"' in p:
                    self.reports.append(p)
            return self._Cur([])
        if "SUM(word_count)" in s:
            return self._Cur([(sum(len(x) for x in self.saved),)])
        return self._Cur([])

    async def executemany(self, *a, **k):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


async def _noop(*a, **k):
    return None


#: 桩事实行（6 元组契约：gt, title, content, confidence, chapter, value_kind）
_FACT_ROWS = [
    ("工程参数", "基坑深度", "基坑深度 8.5m，支护章节 s1 相关", 0.95, "", "measured"),
    ("通用参数", "抗震设防烈度", "7 度", 0.9, "", "measured"),
]

_LONG_AI_TEXT = "## 编制说明\n\n" + ("本章正文内容用于统计超字数。" * 60)

_PREV_TAG = "【前一章正文结尾"


@pytest.fixture()
def stub_env(monkeypatch):
    """打桩任务注册/进度/AI/事实加载；捕获每次 chat 的 messages。"""
    finished: list[tuple] = []
    ckpts: list[dict] = []
    chats: list[list[dict]] = []
    box: dict = {"finished": finished, "ckpts": ckpts, "chats": chats}

    async def _register(*a, **k):
        return "task-x", None

    async def _progress(*a, **k):
        return None

    async def _stats(*a, **k):
        return None

    async def _finish(task_id, status="", message="", **k):
        finished.append((status, message))

    async def _save_ckpt(task_id, payload):
        ckpts.append(payload)

    async def _chat(messages, **k):
        chats.append(list(messages))
        return _LONG_AI_TEXT

    async def _facts(db, scheme_id):
        return [tuple(r) for r in _FACT_ROWS]

    monkeypatch.setattr(sh, "register_task", _register)
    monkeypatch.setattr(sh, "_register_task_exclusive", lambda *a, **k: _register())
    monkeypatch.setattr(sh, "update_progress", _progress)
    monkeypatch.setattr(sh, "update_task_stats", _stats)
    monkeypatch.setattr(sh, "finish_task", _finish)
    monkeypatch.setattr(sh, "_save_content_checkpoint", _save_ckpt)
    monkeypatch.setattr(sh, "settle_global_conn", _noop)
    monkeypatch.setattr(sh, "wait_resume", lambda *a, **k: _noop())
    # ⚠️ 真实 is_stopped 对未注册 task_id 返回 True（防御性 fail-stop），必须接管，
    #    否则一进生成循环即判「用户已停止」（20261005 护栏同坑注释）。
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
    monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)
    monkeypatch.setattr(sh, "chat_with_fallback", _chat)
    monkeypatch.setattr(sh, "_write_review_record", _noop, raising=False)
    monkeypatch.setattr(sh, "_load_facts_rows", _facts)
    return box


async def _run(db, body=None):
    payload = {"mode": "all", "concurrency": 1, "auto_consistency_repair": False}
    payload.update(body or {})
    resp = await sh.generate_content("sc1", _FakeRequest(payload), db=db)
    chunks: list[str] = []
    async for piece in resp.body_iterator:
        chunks.append(piece.decode("utf-8", "replace")
                      if isinstance(piece, bytes) else str(piece))
    text = "".join(chunks)
    events = [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]
    return events, text


def _terminal(events) -> dict:
    for e in reversed(events):
        if e.get("event") in ("completed", "stopped", "error", "degraded"):
            return e
    raise AssertionError(f"未收到终态事件: {[e.get('event') for e in events]}")


def _user_text(chat_messages: list[dict]) -> str:
    return "".join(str(m.get("content") or "") for m in chat_messages
                   if isinstance(m, dict) and m.get("role") == "user")


#: 编排轮（content_plan_user）的固定收口句；正文轮提示词永不含此句。
_PLAN_MARK = "仅输出严格 JSON"


def _content_user_texts(box: dict) -> list[str]:
    """按**文档序**返回正文生成轮的 user 文本（剔除编排轮与其 JSON 修复重试）。

    ✅ 2026-10-23 起 ``content_plan_stage_enabled`` 默认开启（对齐外部 v2
    「编排 → 生成」两阶段工作流），每章先发编排轮（失败还带修复重试）再发
    正文轮 ⇒ ``chats`` 下标不再等于章节序。本护栏关注的是正文轮提示词
    （R105 承接段接线在 content_runtime 装配函数），故以编排标记反向过滤，
    与 D1 开关取值解耦（开/关两态下断言语义一致）。
    """
    out = []
    for m in box["chats"]:
        t = _user_text(m)
        if t and _PLAN_MARK not in t:
            out.append(t)
    return out


# ============================================================================
# A · G1 顺序留痕：派发序记录 → 终态 order_check → 三拼装点同键
# ============================================================================

class TestR105OrderCheckWiring:

    @pytest.mark.asyncio
    async def test_completed_terminal_order_check_ok(self, stub_env):
        db = _DB()
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "completed", (
            f"{[e.get('event') for e in events]} / {text[:400]}")
        oc = term.get("order_check")
        assert isinstance(oc, dict) and oc, f"completed 必须带非空 order_check: {term}"
        assert oc.get("ok") is True, f"派发序应严格等于目录 DFS 序: {oc}"
        assert oc.get("expected_count") == db.leaf_count() \
            and oc.get("dispatched_count") == db.leaf_count(), oc
        # checkpoint 面同源（唯一拼装点纪律）
        ck = stub_env["ckpts"][-1]
        assert ck.get("order_check") == oc, "终态事件与 checkpoint 的 order_check 必须同值"

    @pytest.mark.asyncio
    async def test_stopped_terminal_carries_order_check(self, stub_env, monkeypatch):
        db = _DB()
        monkeypatch.setattr(sh, "is_stopped", lambda tid: len(db.saved) >= 1)
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "stopped", f"应走停止分支: {text[:300]}"
        assert "order_check" in term, f"stopped 必须同键: {term}"
        assert "fact_mapping_stats" in term, f"stopped 必须同键: {term}"

    @pytest.mark.asyncio
    async def test_error_terminal_carries_order_check_and_stats(self, stub_env, monkeypatch):
        async def _boom(messages, **k):
            raise RuntimeError("模拟 AI 全网失败")

        monkeypatch.setattr(sh, "chat_with_fallback", _boom)
        monkeypatch.setattr(sh, "CONTENT_SECTION_RETRIES", 0)
        events, text = await _run(_DB())
        term = _terminal(events)
        assert term["event"] == "error", f"{[e.get('event') for e in events]} / {text[:300]}"
        assert "order_check" in term and "fact_mapping_stats" in term, term

    def test_dispatch_recorded_right_after_semaphore_gate(self):
        """派发序记录点必须在 `async with _run_semaphore:` **之内**、try **之前**：
        过闸=启动（FIFO 放行序=协程创建序=目录序），且只记一次。"""
        src = _src()
        gate = src[src.index("async def guarded_gen"):]
        i_with = gate.index("async with _run_semaphore:")
        i_append = gate.index("_dispatched_order.append", i_with)
        i_try = gate.index("try:", i_with)
        assert i_with < i_append < i_try, "派发记录须紧随过闸、先于业务 try"
        assert src.count("_dispatched_order.append") == 1, "派发序只允许单点记录"

    def test_expected_order_established_only_after_early_exit(self):
        """早退分支（无待生成章节）在事实加载**之前**返回 → 期望序不建立，
        终态 order_check 为空 dict，载荷与功能引入前同构。"""
        src = _src()
        assert src.index("if not leaves:") < src.index('_expected_order[:]'), (
            "期望序必须建立在空章节早退之后（没派发过就不该产校验）")

    def test_order_warning_fires_once(self):
        """失守 WARNING 必须走一次闸（多条收尾路径反复调拼装点不得刷屏）。"""
        src = _src()
        block = src[src.index("def _content_ckpt_payload("):
                    src.index("def _stopped_payload(", src.index("def _content_ckpt_payload("))]
        assert '_order_warned["flag"]' in block, "顺序失守告警必须经一次闸"
        assert block.count("logger.warning") >= 1 and "order_check" in block


# ============================================================================
# B · G5 前驱叶子承接：prev_leaf_summary 注入双装配函数
# ============================================================================

class TestR105PrevLeafContext:

    @pytest.mark.asyncio
    async def test_second_chapter_prompt_contains_prev_tail(self, stub_env):
        db = _DB()
        events, text = await _run(db)
        _terminal(events)
        texts = _content_user_texts(stub_env)
        assert len(texts) >= 2, f"两章正文轮应发生: {len(texts)}"
        first, second = texts[:2]
        assert _PREV_TAG not in first, "首章无文档前驱，不得出现承接段"
        assert _PREV_TAG in second, (
            f"第二章提示词必须含前驱叶子结尾段: {second[:600]}")

    @pytest.mark.asyncio
    async def test_switch_off_falls_back_verbatim(self, stub_env, monkeypatch):
        """content_prev_leaf_context=False → 承接段整段消失（逐字回退）。"""
        monkeypatch.setattr(sh.settings, "content_prev_leaf_context", False)
        db = _DB()
        events, text = await _run(db)
        _terminal(events)
        texts = _content_user_texts(stub_env)
        assert len(texts) >= 2
        assert _PREV_TAG not in texts[1], "开关关闭后不得注入承接段"

    @pytest.mark.asyncio
    async def test_tail_extractor_failure_is_failsoft(self, stub_env, monkeypatch):
        """服务层取片段抛异常 → 回落空串，生成照常完成（承接绝不拖垮正文）。"""
        def _explode(content, *a, **k):
            raise RuntimeError("模拟片段函数炸")

        monkeypatch.setattr(sh, "prev_leaf_tail_summary", _explode)
        db = _DB()
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "completed", f"fail-soft 后生成必须照常完成: {text[:300]}"
        texts = _content_user_texts(stub_env)
        assert len(texts) >= 2
        assert _PREV_TAG not in texts[1], "取片段炸→回落空串，正文轮不得残留承接段"

    def test_both_assembly_calls_receive_prev_leaf(self):
        """分层与非分层两条装配路径都必须传 prev_leaf_summary（同口径纪律）。"""
        src = _src()
        assert src.count("prev_leaf_summary=_prev_leaf_tail_for(leaf)") == 2, (
            "build_chapter_layered_messages 与 build_chapter_user_content "
            "两处调用点必须各传一次")

    def test_pure_function_tail_shape(self):
        """服务层纯函数契约：≤max_chars、围栏剔除（接线两侧依赖该语义）。"""
        tail = prev_leaf_tail_summary("开头段。\n\n" + "结尾承接段。" * 100)
        assert tail and len(tail) <= 300
        assert prev_leaf_tail_summary("```mermaid\ngraph TD;A-->B;\n```") == ""


# ============================================================================
# C · G3 注入审计：单一出口 + 留痕落库 + 逐字节一致
# ============================================================================

class TestR105FactInjectionAudit:

    def test_audit_rendered_text_identical_to_bare_call(self):
        """fact_injection_audit 与裸 _render_facts_text 默认参数同值 → 逐字节一致
        （正文替换零文案漂移的结构性保证）。"""
        leaf = {"title": "支护章节 s1", "description": "开挖"}
        for rows in ([], _FACT_ROWS):
            assert fact_injection_audit(rows)["rendered_text"] \
                == _render_facts_text(rows)
            assert fact_injection_audit(rows, leaf=leaf, chapter="plan")["rendered_text"] \
                == _render_facts_text(rows, relevant_to=leaf, chapter="plan")

    def test_audit_reports_budget_drops(self):
        """预算远少于事实量时审计必须点名被丢事实（留痕即观测）。"""
        rows = [("组", f"事实{i}", "内容" * 60, 0.9, "", "measured")
                for i in range(300)]
        au = fact_injection_audit(rows, max_total=100, per_fact=50)
        assert au["budget_dropped_count"] == len(au["budget_dropped"])
        assert au["budget_dropped"], "300 条事实挤 100 字预算必有丢弃"
        assert au["rendered_chars"] == len(au["rendered_text"])

    @pytest.mark.asyncio
    async def test_checkpoint_carries_injection_summary(self, stub_env):
        db = _DB()
        events, text = await _run(db)
        _terminal(events)
        ck = stub_env["ckpts"][-1]
        summ = ck.get("fact_injection_summary")
        assert isinstance(summ, dict) and summ, f"ckpt 必须带注入留痕汇总: {ck}"
        assert summ["sections"] == db.leaf_count(), f"全部叶子都应有逐章留痕: {summ}"
        for key in ("budget_dropped_total", "budget_dropped_sections"):
            assert key in summ

    @pytest.mark.asyncio
    async def test_persisted_report_carries_fact_audit(self, stub_env):
        db = _DB()
        events, text = await _run(db)
        _terminal(events)
        assert db.reports, "落库报告参数必须含 fact_audit 留痕 JSON"
        rep = json.loads(db.reports[0])
        fa = rep.get("fact_audit")
        assert isinstance(fa, dict), f"报告必须有 fact_audit 键: {list(rep)}"
        for key in ("injected_facts", "rendered_chars",
                    "budget_dropped", "budget_dropped_count"):
            assert key in fa, f"留痕缺键 {key}: {fa}"

    def test_bare_render_facts_text_has_no_call_site(self):
        """正文生成链单一出口纪律：generate_content 内不得再裸调 _render_facts_text
        （re-export 仅留给 tests，import 行带 noqa 说明）。"""
        assert "_render_facts_text(" not in _src(), (
            "调用点必须走 fact_injection_audit，防再次漂移出第二套口径")
        # ✅ R122（2026-10-21）：core/chapter/plain 三分支之外新增第 4 处——
        #    content_plan_selected_facts_inject 门控内的「选中事实重算」，
        #    同样走 fact_injection_audit 出口（纪律不变，计数+1）。
        assert _src().count("fact_injection_audit(") == 4, (
            "core/chapter/plain 三分支 + R122-G1 门控重算各一次，不多不少")


# ============================================================================
# D · G4 章节→事实映射表：全量进 checkpoint、汇总进 SSE 终态
# ============================================================================

class TestR105ChapterFactMapping:

    @pytest.mark.asyncio
    async def test_checkpoint_carries_full_mapping(self, stub_env):
        db = _DB()
        events, text = await _run(db)
        term = _terminal(events)
        mapping = stub_env["ckpts"][-1].get("fact_mapping")
        assert isinstance(mapping, list) and len(mapping) == db.leaf_count(), (
            f"映射表须与 leaves 同序全量: {mapping}")
        leaf_ids = [s["id"] for s in db._sections if int(s["level"]) >= 4]
        assert [m["section_id"] for m in mapping] == leaf_ids, mapping
        for m in mapping:
            assert set(m) >= {"section_id", "title", "fact_titles", "fact_count"}
            assert m["fact_count"] == len(m["fact_titles"]) and m["fact_count"] >= 1
        # SSE 终态只带汇总（长方案不把几十 KB 全量表塞进事件）
        stats = term.get("fact_mapping_stats")
        assert isinstance(stats, dict) and stats, f"终态必须带映射汇总: {term}"
        assert stats["sections"] == len(mapping)
        assert stats["fact_refs"] == sum(m["fact_count"] for m in mapping)
        assert stats["sections_with_facts"] + stats["sections_without_facts"] \
            == stats["sections"]

    def test_mapping_uses_same_criterion_as_injection(self):
        """chapter_fact_mapping 与 _filter_facts_rows 同一判据（声明式视图，
        不得漂移出第二套归类规则）。"""
        leaf = {"title": "支护章节 s1", "description": "开挖"}
        mapping = chapter_fact_mapping([leaf], _FACT_ROWS)
        assert len(mapping) == 1
        assert mapping[0]["fact_titles"] == [
            str(r[1]) for r in _filter_facts_rows(list(_FACT_ROWS), leaf) if r[1]]

    def test_summarize_dirty_input_degrades(self):
        """汇总纯函数：脏入参不抛 —— 非 dict 行被过滤，int 炸则降级 degraded。"""
        s = summarize_fact_mapping([{"bogus": 1}, None, 3],
                                   {"x": {"budget_dropped_count": 2}})
        assert s["sections"] == 1, "非 dict 脏行过滤，dict 行计入"
        assert s["budget_dropped_total"] == 2
        bad = summarize_fact_mapping([{"fact_count": "abc"}], None)
        assert bad.get("degraded") is True, "int 炸必须走降级形态而非上抛"
        s2 = summarize_fact_mapping(None, None)
        assert s2["sections"] == 0 and s2["fact_refs"] == 0
        # 正常口径：丢弃统计来自逐章留痕
        s3 = summarize_fact_mapping(
            [{"section_id": "a", "fact_count": 2}, {"section_id": "b", "fact_count": 0}],
            {"a": {"budget_dropped_count": 3}, "b": {"budget_dropped_count": 0}})
        assert s3["sections"] == 2 and s3["sections_with_facts"] == 1
        assert s3["fact_refs"] == 2 and s3["budget_dropped_total"] == 3
        assert s3["budget_dropped_sections"] == 1


# ============================================================================
# E · fail-soft：留痕/映射/校验任何一环炸都不阻断生成与落库
# ============================================================================

class TestR105FailSoftDiscipline:

    @pytest.mark.asyncio
    async def test_mapping_failure_degrades_to_empty(self, stub_env, monkeypatch):
        def _explode(*a, **k):
            raise RuntimeError("模拟映射表计算炸")

        monkeypatch.setattr(sh, "chapter_fact_mapping", _explode)
        db = _DB()
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "completed", f"映射炸不得阻断生成: {text[:300]}"
        ck = stub_env["ckpts"][-1]
        assert ck.get("fact_mapping") == [], "降级形态必须是空表"
        assert term["fact_mapping_stats"]["sections"] == 0

    @pytest.mark.asyncio
    async def test_order_verify_failure_degrades_to_empty(self, stub_env, monkeypatch):
        def _explode(*a, **k):
            raise RuntimeError("模拟顺序校验炸")

        monkeypatch.setattr(sh, "verify_generation_order", _explode)
        db = _DB()
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "completed", f"校验炸不得阻断终态: {text[:300]}"
        assert term.get("order_check") == {}, "校验异常必须降级为空 dict"

    @pytest.mark.asyncio
    async def test_zero_facts_still_records_audit(self, stub_env, monkeypatch):
        """无事实方案：留痕容器仍逐章记录（空 injected_facts），
        汇总 sections 计数不为 0 —— 「没调用」与「没留痕」必须可区分。"""
        async def _no_facts(db, scheme_id):
            return []

        monkeypatch.setattr(sh, "_load_facts_rows", _no_facts)
        db = _DB()
        events, text = await _run(db)
        _terminal(events)
        ck = stub_env["ckpts"][-1]
        assert ck["fact_injection_summary"]["sections"] == db.leaf_count()
        assert all(m["fact_count"] == 0 for m in ck["fact_mapping"])


# ============================================================================
# F · 字节体检（本仓工作树 CRLF 约定，防 A/B 与编辑工具漂移行尾）
# ============================================================================

class TestR105ByteHygiene:

    @pytest.mark.parametrize("path", [_SH_FILE, _CU_FILE])
    def test_crlf_no_bom_no_replacement_char(self, path: Path):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 不得带 UTF-8 BOM"
        bare_lf = raw.replace(b"\r\n", b"").count(b"\n")
        assert bare_lf == 0, f"{path.name} 必须保持纯 CRLF 工作树约定（裸 LF={bare_lf}）"
        assert b"\r" not in raw.replace(b"\r\n", b""), f"{path.name} 不得有孤立 CR"
        assert raw.decode("utf-8").count("\ufffd") == 0, f"{path.name} 不得含 FFFD"
