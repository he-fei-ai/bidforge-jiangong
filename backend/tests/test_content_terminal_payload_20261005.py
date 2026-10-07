"""正文生成模块深度审计 · 终态载荷与 checkpoint 一致性护栏（2026-10-05 · R43）

本轮审计发现的 5 项缺陷同属**一个病根**：同一份终态/成果载荷在**多条收尾路径
各自手写**，改一处漏其余（本仓 AGENTS.md §4.3/§4.7/§4.13/§4.21 反复记录的
「同一判据在 2~3 处各自实现」）。因此本文件既锁**行为**（端到端跑真实
SSE 流断言载荷字段），也锁**结构**（唯一拼装点必须存在、调用点必须走它），
防止后人把字段从 helper 里删掉而调用点看不出问题。

覆盖：
  R1  checkpoint 统计量（words / run_words / over_count）在**全部 5 条**收尾路径
      都由唯一拼装点回填 —— 修复前只有 completed 路径写，导致停止/取消/异常/
      断线重挂后前端永远读到 0 字。
  R2  两条 stopped 路径（用户停止 / CancelledError）字段完全一致，
      都带 progress 与 standard_summary —— 修复前取消路径缺这两项。
  R3  两条 error 路径（全章失败 / 整批异常）字段完全一致，
      都带 total / failed_count / progress / standard_summary，
      且有失败明细时必带 failed_sections —— 修复前整批异常路径全丢。
  R4  章节树查询的 R13 判空（db.execute 返回 None 时降级为「无可生成章节」
      而不是整批 AttributeError 500）。
  R5  编号元数据预取（load_scheme_section_index）在正文链路真正接线，
      消除逐章 4 次查库的 N+1。
  R6  前端 onSseEvent 的 error 分支必须消费 failed_sections
      （静态锁：页面 11k 行无法组件级单测，锁源码接线形态）。
"""

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from app.routers import sse_handlers as sh

_FRONTEND_PAGE = (Path(__file__).resolve().parents[2] / "frontend"
                  / "src" / "pages" / "SchemeWorkbenchPage.tsx")


def _src() -> str:
    return inspect.getsource(sh.generate_content)


# ============================================================================
# 端到端 harness（沿用 test_content_fence_contract_20260929 的桩 DB 思路）
# ============================================================================

class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _MultiSectionDB:
    """最小桩 DB：2 个叶子章节 + 可控的 execute 返回值。"""

    def __init__(self, sections=None, *, sections_cursor_none=False):
        self.saved: list[str] = []
        self._sections = sections if sections is not None else [
            self._sec("s1", "1.1", 1, "", 0, 20),
            self._sec("s2", "1.2", 1, "", 1, 20),
        ]
        self._sections_cursor_none = sections_cursor_none

    @staticmethod
    def _sec(sid, num, level, parent, sort, budget):
        return {
            "id": sid, "scheme_id": "sc1", "title": f"章节 {sid}",
            "level": level, "parent_id": parent, "sort_order": sort,
            "word_budget": budget, "content": "", "status": "empty",
            "description": "d", "outline_json": json.dumps({"id": num},
                                                          ensure_ascii=False),
            "generation_standard": "", "review_status": "", "word_count": 0,
        }

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

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
            if self._sections_cursor_none:
                return None          # 模拟 R13：db.execute 返回 None
            return self._Cur([dict(x) for x in self._sections])
        if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
            self.saved.append(params[0])
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


async def _coro(value):
    """把普通值包成协程（便于用 lambda 打桩 AI）。"""
    return value


#: 正文足够长（> 预算 × 1.3）以触发 word_status='over'，从而 over_count > 0
_LONG_AI_TEXT = "## 编制说明\n\n" + ("本章正文内容用于统计超字数。" * 60)


@pytest.fixture()
def stub_env(monkeypatch):
    """打桩任务注册/进度/AI；返回 (finished 列表, ckpt 列表, db 引用槽)。"""
    finished: list[tuple] = []
    ckpts: list[dict] = []
    box: dict = {}

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
        return _LONG_AI_TEXT

    monkeypatch.setattr(sh, "register_task", _register)
    monkeypatch.setattr(sh, "_register_task_exclusive",
                        lambda *a, **k: _register())
    monkeypatch.setattr(sh, "update_progress", _progress)
    monkeypatch.setattr(sh, "update_task_stats", _stats)
    monkeypatch.setattr(sh, "finish_task", _finish)
    monkeypatch.setattr(sh, "_save_content_checkpoint", _save_ckpt)
    monkeypatch.setattr(sh, "settle_global_conn", _noop)
    monkeypatch.setattr(sh, "wait_resume", lambda *a, **k: _noop())
    # ⚠️ 真实 is_stopped 对**未注册**的 task_id 返回 True（防御性 fail-stop），
    #    桩里不接管它会让每个用例一进生成循环就判「用户已停止」——
    #    踩过一次：终态恒为 stopped、progress 停在 0.02，误判成生产缺陷。
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
    monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)
    monkeypatch.setattr(sh, "chat_with_fallback", _chat)
    monkeypatch.setattr(sh, "_write_review_record", _noop, raising=False)
    box["finished"] = finished
    box["ckpts"] = ckpts
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
        if e.get("event") in ("completed", "stopped", "error"):
            return e
    raise AssertionError(f"未收到终态事件: {[e.get('event') for e in events]}")


# ============================================================================
# R1 · checkpoint 统计量在全部收尾路径都由唯一拼装点回填
# ============================================================================

class TestR1CheckpointStatsSingleSource:

    def test_payload_helper_backfills_stats(self):
        """唯一拼装点内部必须回填 words / run_words / over_count。"""
        src = _src()
        i = src.index("def _content_ckpt_payload(")
        # 窗口取到下一个顶层 def 为止，与实现长度解耦（§5.14 判据锚点）
        block = src[i:src.index("\n        def ", i + 10)]
        assert '_ckpt["words"]' in block, "拼装点必须回填 words"
        assert '_ckpt["run_words"]' in block, "拼装点必须回填 run_words"
        assert '_ckpt["over_count"]' in block, "拼装点必须回填 over_count"

    def test_no_call_site_writes_stats_by_hand(self):
        """调用点不得再各自手写统计量（那正是本次漂移的来源）。"""
        src = _src()
        assert src.count('_ckpt["run_words"]') == 1, (
            "run_words 只允许在唯一拼装点赋值一次")
        assert src.count('_ckpt["over_count"]') == 1, (
            "over_count 只允许在唯一拼装点赋值一次")
        # words 另有一处在 _persist_section 里累加进度（_ckpt["done"] 旁），
        # 那是「每落库一章就同步一次」的增量写入，与终态回填语义不同，允许存在。
        assert src.count('_ckpt["words"] = _prog.get("words", 0)') == 0, (
            "终态路径不得再手写 _ckpt['words']（应统一由拼装点回填）")

    @pytest.mark.asyncio
    async def test_completed_path_carries_real_stats(self, stub_env):
        db = _MultiSectionDB()
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "completed", (
            f"{[e.get('event') for e in events]} / {text[:400]}")
        ck = stub_env["ckpts"][-1]
        assert ck["words"] > 0 and ck["run_words"] > 0, (
            f"completed checkpoint 必须带真实字数: {ck}")
        assert ck["over_count"] > 0, f"超字数章数必须回填: {ck}"
        # 在线事件与 checkpoint 必须同口径（前端两处都读）
        assert term["run_words"] == ck["run_words"], (
            f"completed 事件与 checkpoint 的 run_words 必须一致: "
            f"{term.get('run_words')} vs {ck.get('run_words')}")

    @pytest.mark.asyncio
    async def test_stopped_path_carries_stats_and_progress(self, stub_env, monkeypatch):
        """用户点停止：checkpoint 与 stopped 事件都必须带真实统计量。"""
        db = _MultiSectionDB()

        def _stop_after_first(tid):
            # 第一章落库之后即视为「用户已停止」
            return len(db.saved) >= 1

        monkeypatch.setattr(sh, "is_stopped", _stop_after_first)
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "stopped", (
            f"应走停止分支: {[e.get('event') for e in events]} / {text[:400]}")
        assert isinstance(term.get("progress"), (int, float)), (
            f"stopped 事件必须带 progress: {term}")
        assert "standard_summary" in term, (
            f"stopped 事件必须带 standard_summary: {term}")
        ck = stub_env["ckpts"][-1]
        assert ck["words"] > 0 and ck["run_words"] > 0, (
            f"停止路径 checkpoint 必须带真实字数（修复前恒 0）: {ck}")
        assert ck["over_count"] > 0, (
            f"停止路径 checkpoint 必须带超字数章数（修复前恒 0）: {ck}")
        assert ck["done"] >= 1, ck


# ============================================================================
# R2 · 两条 stopped 路径字段一致
# ============================================================================

class TestR2StoppedPayloadParity:

    def _helper_block(self) -> str:
        src = _src()
        i = src.index("def _stopped_payload(")
        return src[i:src.index("\n        def ", i + 10)]

    def test_helper_carries_all_fields(self):
        block = self._helper_block()
        for field in ("'event': 'stopped'", "'progress': _p",
                      "'failed_count': max(total - len(done_ids), 0)",
                      "'failed_sections'", "'standard_summary': _std_sum"):
            assert field in block, f"stopped 拼装点缺少 {field}"

    def test_both_stopped_paths_go_through_helper(self):
        src = _src()
        # 用户停止
        assert "_stopped_payload('用户已停止', stop_progress)" in src
        # CancelledError
        assert "_stopped_payload('任务已取消')" in src
        # 不得再有内联手拼的 stopped dict
        assert "'event':'stopped'" not in src, (
            "两条 stopped 路径必须都走 _stopped_payload，不得再内联手拼")

    def test_helper_progress_falls_back_to_weighted(self):
        """省略 progress 时必须回落到加权进度（并过不可回退护栏）。"""
        block = self._helper_block()
        assert "_progress_now() if progress is None" in block


# ============================================================================
# R3 · 两条 error 路径字段一致
# ============================================================================

class TestR3ErrorPayloadParity:

    def _helper_block(self) -> str:
        src = _src()
        i = src.index("def _error_payload(")
        # ⚠️ _error_payload 是 event_stream 内**最后一个**嵌套 def（其后是
        #    `_db_write_lock = ...`），没有下一个 "def " 可作边界 → 用下一个
        #    顶层赋值语句收尾。与实现长度解耦（§5.14 判据锚点）。
        j = src.index("_db_write_lock = asyncio.Lock()", i)
        return src[i:j]

    def test_helper_carries_all_fields(self):
        block = self._helper_block()
        for field in ("'event': 'error'", "'progress': _p", "'total': total",
                      "'failed_count': max(total - len(done_ids), 0)",
                      "'standard_summary': _std_sum", "'failed_sections'"):
            assert field in block, f"error 拼装点缺少 {field}"

    def test_both_error_paths_go_through_helper(self):
        src = _src()
        assert "_error_payload(_all_msg)" in src, "全章失败路径必须走拼装点"
        assert "_error_payload(str(e))" in src, "整批异常路径必须走拼装点"
        assert "'event':'error'" not in src, (
            "两条 error 路径必须都走 _error_payload，不得再内联手拼")

    @pytest.mark.asyncio
    async def test_all_failed_error_carries_details(self, stub_env, monkeypatch):
        """全章失败：error 事件必须带失败明细（供前端逐章重试）。"""
        db = _MultiSectionDB(sections=[
            _MultiSectionDB._sec("s1", "1.1", 1, "", 0, 20)])

        async def _boom(messages, **k):
            raise RuntimeError("模拟 AI 全网失败")

        monkeypatch.setattr(sh, "chat_with_fallback", _boom)
        # 关掉重试，否则 2 次尝试之间有 6s 退避 sleep（用例会慢 12s）
        monkeypatch.setattr(sh, "CONTENT_SECTION_RETRIES", 0)
        events, text = await _run(db)
        term = _terminal(events)
        assert term["event"] == "error", (
            f"{[e.get('event') for e in events]} / {text[:400]}")
        assert term["failed_count"] == 1
        assert term.get("failed_sections"), f"必须带失败明细: {term}"
        assert isinstance(term.get("progress"), (int, float))
        assert "standard_summary" in term
        assert term["total"] == 1


# ============================================================================
# R4 · 章节树查询的 R13 判空
# ============================================================================

class TestR4SectionsTreeR13Guard:

    def test_source_has_r13_guard(self):
        src = _src()
        i = src.index('"SELECT * FROM sections WHERE scheme_id=? ORDER BY sort_order"')
        window = src[i:i + 600]
        assert "if cur is not None" in window, (
            "章节树查询后必须判空（R13：db.execute 可能返回 None）")

    @pytest.mark.asyncio
    async def test_none_cursor_degrades_gracefully(self, stub_env):
        """db.execute 返回 None 时不得整批 500，应降级为「没有待生成的章节」。"""
        db = _MultiSectionDB(sections_cursor_none=True)
        events, text = await _run(db)
        names = [e.get("event") for e in events]
        assert "error" not in names, (
            f"R13 命中不得整批失败: {names} / {text[:400]}")
        term = _terminal(events)
        assert term["event"] == "completed"
        assert term.get("message") == "没有待生成的章节"
        assert ("completed", ) == tuple(
            s for s, _ in stub_env["finished"])


# ============================================================================
# R5 · 编号元数据预取接线（消除逐章 N+1）
# ============================================================================

class TestR5NumberingIndexWiring:

    def test_index_is_prefetched_once(self):
        src = _src()
        assert src.count("load_scheme_section_index(db, scheme_id)") == 1, (
            "编号索引必须且只能预取一次（生成前）")

    def test_both_numbering_calls_receive_index(self):
        src = _src()
        for fn in ("normalize_section_content_subheadings",
                   "validate_section_content_numbering"):
            i = src.index(fn + "(")
            window = src[i:i + 400]
            assert "index=_numbering_index" in window, (
                f"{fn} 必须传预取索引（否则逐章 2 次查库的 N+1 依旧）")

    def test_module_imports_the_prefetch_helper(self):
        tree = ast.parse(inspect.getsource(sh))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.services.numbering":
                if any(a.name == "load_scheme_section_index" for a in node.names):
                    found = True
        assert found, "必须在模块顶层 import load_scheme_section_index"


# ============================================================================
# R6 · CON-06 跨章搬运检测整链失效（nonlocal 缺失）
# ============================================================================

class TestR6CrossSectionCopyNotDead:

    def test_persist_section_declares_nonlocal(self):
        """`_persist_section` 必须 `nonlocal _crossdup_snapshot`。

        否则该函数内对同名变量的**普通名字赋值**会在编译期把它判定为局部变量，
        先执行的 `if _crossdup_snapshot is None:` 每次都抛 UnboundLocalError →
        被外层 fail-soft 吞掉 → CON-06 检测从未真正执行过一次。
        """
        tree = ast.parse(_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) \
                    and node.name == "_persist_section":
                target = node
                break
        assert target is not None, "未找到 _persist_section"
        nonlocal_names = set()
        for n in ast.walk(target):
            if isinstance(n, ast.Nonlocal):
                nonlocal_names.update(n.names)
        assert "_crossdup_snapshot" in nonlocal_names, (
            "_persist_section 必须 nonlocal _crossdup_snapshot"
            "（否则跨章搬运检测是死代码）")

    def test_no_load_before_store_without_nonlocal(self):
        """通用不变量：本函数内被读取的外层变量不得以普通赋值形式遮蔽。

        这是本条缺陷的**病根形态**，比单点锁 `_crossdup_snapshot` 更强：
        日后再有人往 `_persist_section` 里加第二个「先读后写」的闭包变量，
        本用例会立刻失败。
        """
        tree = ast.parse(_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) \
                    and node.name == "_persist_section":
                target = node
                break
        assert target is not None
        nonlocal_names = set()
        global_names = set()
        for n in ast.walk(target):
            if isinstance(n, ast.Nonlocal):
                nonlocal_names.update(n.names)
            elif isinstance(n, ast.Global):
                global_names.update(n.names)
        # 形参在进入函数时即绑定，函数体内先读后写是同一局部变量的合法复用
        # （如 _persist_section(section_id, content) 内先读 content 再以规范化
        # 结果回赋），绝不可能触发 UnboundLocalError，必须排除。
        _a = target.args
        arg_names = {a.arg for a in (*_a.posonlyargs, *_a.args, *_a.kwonlyargs)}
        if _a.vararg is not None:
            arg_names.add(_a.vararg.arg)
        if _a.kwarg is not None:
            arg_names.add(_a.kwarg.arg)
        stores: dict[str, int] = {}
        loads: dict[str, int] = {}

        # ✅ 作用域感知（2026-10-07 修误报）：只统计 _persist_section **直接函数体**
        #    作用域里的裸名。旧实现 ast.walk 不区分析嵌套作用域，会把生成器/列表等
        #    推导式的迭代变量误判为「先读后写的外层变量」——例如删图日志里的
        #    `"、".join(label(d) for d in _chart_dropped)`：值表达式对 d 的 Load 行
        #    恰好早于 `for d` target 的 Store 行，但推导式在 Py3 有独立作用域，
        #    二者是同一推导式内的合法自绑定，绝不产生 UnboundLocalError。
        class _DirectScopeNames(ast.NodeVisitor):
            def _skip(self, node):  # 嵌套作用域：整棵子树不下钻
                return

            visit_FunctionDef = _skip
            visit_AsyncFunctionDef = _skip
            visit_Lambda = _skip
            visit_ListComp = _skip
            visit_SetComp = _skip
            visit_DictComp = _skip
            visit_GeneratorExp = _skip

            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Store):
                    stores.setdefault(node.id, node.lineno)
                elif isinstance(node.ctx, ast.Load):
                    loads.setdefault(node.id, node.lineno)

        walker = _DirectScopeNames()
        for _stmt in target.body:  # 从函数体直接语句起步，target 自身不触发跳过
            walker.visit(_stmt)
        bad = [
            f"{name}(store@{stores[name]} < load@{loads[name]})"
            for name in stores
            if name in loads and loads[name] < stores[name]
            and name not in nonlocal_names
            and name not in global_names
            and name not in arg_names
            and not name.startswith("_")
        ]
        assert not bad, (
            "以下外层变量在本函数内「先读后写」且未声明 nonlocal，"
            f"运行期必抛 UnboundLocalError 并被 fail-soft 静默吞掉: {bad}")

    def test_scope_walker_ignores_comprehension_but_catches_real_shadow(self):
        """防改瞎：作用域过滤只放过推导式迭代变量，真实函数级遮蔽仍须抓到。"""

        def shadowed_names(code: str):
            fn = ast.parse(code).body[0]
            stores: dict[str, int] = {}
            loads: dict[str, int] = {}

            class _W(ast.NodeVisitor):
                def _skip(self, node):
                    return

                visit_FunctionDef = _skip
                visit_AsyncFunctionDef = _skip
                visit_Lambda = _skip
                visit_ListComp = _skip
                visit_SetComp = _skip
                visit_DictComp = _skip
                visit_GeneratorExp = _skip

                def visit_Name(self, node):
                    if isinstance(node.ctx, ast.Store):
                        stores.setdefault(node.id, node.lineno)
                    elif isinstance(node.ctx, ast.Load):
                        loads.setdefault(node.id, node.lineno)

            w = _W()
            for _s in fn.body:
                w.visit(_s)
            _aa = fn.args
            params = {a.arg for a in (*_aa.posonlyargs, *_aa.args, *_aa.kwonlyargs)}
            return [n for n in stores
                    if n in loads and loads[n] < stores[n]
                    and n not in params and not n.startswith("_")]

        # 推导式迭代变量：值表达式引用与 for target 同属独立作用域 → 不得报
        assert shadowed_names(
            "async def f():\n    return [label(x) for x in items]\n") == []
        # 嵌套函数内的同名遮蔽也不得计入外层函数
        assert shadowed_names(
            "async def f():\n"
            "    async def inner():\n"
            "        q = q + 1\n"
            "    return inner\n") == []
        # 形参先读后写（规范化后回赋）是同一局部变量的合法复用 → 不得报
        assert shadowed_names(
            "async def f(content):\n    use(content)\n    content = clean(content)\n"
        ) == []
        # 真实函数级「先读后写」遮蔽必须仍被抓到
        assert shadowed_names(
            "async def f():\n    if flag:\n        use(z)\n    z = 1\n") == ["z"]

    @pytest.mark.asyncio
    async def test_con06_finding_actually_reaches_the_report(self, stub_env,
                                                              monkeypatch):
        """端到端：两章正文雷同 → 落库报告里必须真的出现 CON-06 finding。

        修复前该链路因 UnboundLocalError 从未执行，报告里恒无 CON-06 ——
        本用例是「功能真的活着」的证据，而不只是静态锁。
        """
        dup = ("## 编制说明\n\n"
               "本节明确施工现场的临时用电管理制度与三级配电保护要求，"
               "并对配电箱设置防雨棚与接地保护。\n")
        monkeypatch.setattr(sh, "chat_with_fallback",
                            lambda *a, **k: _coro(dup))
        db = _MultiSectionDB(sections=[
            _MultiSectionDB._sec("s1", "1.1", 1, "", 0, 5000),
            _MultiSectionDB._sec("s2", "1.2", 1, "", 1, 5000),
        ])
        db.reports: list[str] = []

        orig_exec = db.execute

        async def _exec(sql, params=()):
            s = str(sql)
            if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
                db.saved.append(params[0])
                if len(params) > 5 and params[5]:
                    db.reports.append(params[5])
                return db._Cur([])
            return await orig_exec(sql, params)

        db.execute = _exec
        events, text = await _run(db)
        assert len(db.reports) == 2, (
            f"两章都应落库并写报告: {len(db.reports)} / "
            f"{[e.get('event') for e in events]}")
        joined = "".join(db.reports)
        assert "CON-06" in joined, (
            "落库报告里必须出现 CON-06 跨章搬运 finding（修复前恒无，"
            f"因 UnboundLocalError 被 fail-soft 吞掉）: {joined[:500]}")

    @pytest.mark.asyncio
    async def test_crosscheck_can_still_be_disabled(self, stub_env, monkeypatch):
        """开关关闭时不得产生 CON-06（向后兼容红线）。"""
        dup = ("## 编制说明\n\n"
               "本节明确施工现场的临时用电管理制度与三级配电保护要求，"
               "并对配电箱设置防雨棚与接地保护。\n")
        monkeypatch.setattr(sh, "chat_with_fallback",
                            lambda *a, **k: _coro(dup))
        monkeypatch.setattr(sh.settings, "content_crosscheck_duplicate", False,
                            raising=False)
        db = _MultiSectionDB(sections=[
            _MultiSectionDB._sec("s1", "1.1", 1, "", 0, 5000),
            _MultiSectionDB._sec("s2", "1.2", 1, "", 1, 5000),
        ])
        db.reports = []
        orig_exec = db.execute

        async def _exec(sql, params=()):
            s = str(sql)
            if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
                db.saved.append(params[0])
                if len(params) > 5 and params[5]:
                    db.reports.append(params[5])
                return db._Cur([])
            return await orig_exec(sql, params)

        db.execute = _exec
        await _run(db)
        assert "CON-06" not in "".join(db.reports)


# ============================================================================
# R7 · 前端 error 分支必须消费 failed_sections
# ============================================================================

class TestR6FrontendErrorConsumesFailedSections:

    def _page(self) -> str:
        assert _FRONTEND_PAGE.exists(), f"未找到前端页面: {_FRONTEND_PAGE}"
        return _FRONTEND_PAGE.read_text(encoding="utf-8")

    def test_on_sse_event_error_branch_merges_failed_sections(self):
        src = self._page()
        i = src.index("const onSseEvent = useCallback(")
        block = src[i:src.index("}, []);", i)]
        assert 'evt.event === "error"' in block
        assert "failed_sections" in block, (
            "onSseEvent 的 error 分支必须消费 failed_sections"
            "（否则后端下发的逐章失败明细被整段丢弃）")
        assert "mergeFailedSectionsIntoRef" in block

    def test_helper_is_defined_before_on_sse_event(self):
        """useCallback([]) 捕获首帧闭包 → helper 必须在 onSseEvent 之上定义。"""
        src = self._page()
        assert (src.index("const mergeFailedSectionsIntoRef = useCallback(")
                < src.index("const onSseEvent = useCallback("))

    def test_helper_only_touches_ref_and_stable_setter(self):
        src = self._page()
        i = src.index("const mergeFailedSectionsIntoRef = useCallback(")
        block = src[i:src.index("}, []);", i)]
        assert "sectionLogsRef.current" in block
        assert "setSectionLogs" in block
        # 不得依赖任何随渲染变化的闭包变量（否则 useCallback([]) 会读到过期值）
        assert "selectedSection" not in block

    def test_local_merge_failed_sections_delegates(self):
        """页面内的 mergeFailedSections 必须委托给 helper（单一实现）。"""
        src = self._page()
        i = src.index("const mergeFailedSections = (list: any) => {")
        block = src[i:i + 200]
        assert "mergeFailedSectionsIntoRef(list)" in block, (
            "不得保留第二份合并实现（同一判据两处写 = 分叉病根）")