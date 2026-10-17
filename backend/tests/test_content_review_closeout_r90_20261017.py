"""正文生成 + 审核与预检联合审计收口护栏（R90 · 2026-10-17）。

## 本轮定稿的 4 个缺陷（全部有代码依据 + 探针实证）

| 编号 | 级别 | 位置 | 现象 |
|---|---|---|---|
| BUG-RV-1 | **P0** | `preflight_engine._finding` / `check_duplication` | `TypeError` → CON-06 全丢 + consistency 分数**虚高** + 维度污染 |
| BUG-CG-1 | **P0** | `sse_handlers.generate_content` 空章节早退 | `yield completed` 先于 `finish_task` → 成功任务被改写成 stopped |
| BUG-RV-2 | **P1** | `export.collect_export_issues` / `compliance` 总检 | DB 读失败静默降级 → `failed_sources` 漏 `export_check` → 分数虚高 |
| BUG-RV-4 | **P1** | `services/repair_record` | 9 处裸 `db.execute` + 写路径不看结果 → confirm/rollback **假成功** |

### BUG-RV-1 的三重后果（探针 `artifacts/r90/probe_rv1.py` 实测）

修复前 `run_preflight` 输出：

    engine_error: 1  |  CON-06 findings: []  |  unknown_dimension_count: 1
    consistency score: 100.0      ← 检查器崩溃反而「更好」

修复后：

    engine_error: 0  |  CON-06 findings: ['CON-06-1','CON-06-2','CON-06']
    unknown_dimension_count: 0    |  consistency score: 82.0（真实值）

⚠️ 根因是两处**独立**缺陷叠加，只修其一仍红：
  ① `_finding()` 没有 `severity` 形参（调用点传了 → TypeError）；
  ② `CON-06-TRUNC` 这种自造编号 **无法**被 `_resolve_base_rule` 解析
     （只剥离 `-<数字>` 后缀，遇 `-TRUNC` 直接 break → 返回 None）
     → 即便补上形参，`dimension=""` 仍会污染 `unknown_dimension_count`。

### BUG-CG-1 为什么既有护栏没抓到（§5.14「判据锚点」同族）

`tests/test_content_deep_audit.py::TestTerminalOrdering` 逐路径断言
「checkpoint → finish_task → yield」，但**只锚了 3 条路径**，且每条的锚点都
绑死该路径独有的字符串（`_terminal_status` / `"用户已停止"` / `"任务已取消"`）。
第 4 条「空章节」路径的 yield 载荷是内联字典字面量
（`json.dumps({'event':'completed',...})`），而 `test_no_finish_task_after_completed_yield`
用的是 `src.index("yield f\"data: {json.dumps(completed_payload")` —— `index`
取**首次**出现，四条路径里只有正常完成那条含 `completed_payload` ⇒
空章节路径落在两个断言的**覆盖窗口之外**，缺陷长期存活。

⇒ 本文件 C 组不逐路径抄锚点，而是**枚举全部终态 yield**后逐个断言
「其后再无 finish_task」，把这类不变量从「点名式」升级为「全量式」。
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from app.services import audit_rules as ar
from app.services import duplicate_detection as dd
from app.services import preflight_engine as pf
from app.services.audit_rules import SEVERITY_ORDER, validate_rule_registry
from app.routers import export as ex
from app.routers import sse_handlers as sh

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
_ENGINE_PY = _BACKEND / "app" / "services" / "preflight_engine.py"
_REPAIR_RECORD_PY = _BACKEND / "app" / "services" / "repair_record.py"


# ---------------------------------------------------------------------------
# 语料：制造多组「成段照抄」的章节（骨架归一后互不相同，故产生多个 group）
# ---------------------------------------------------------------------------
def _dup_sections() -> list[dict]:
    filler = "本章另按图纸与规范要求组织施工，验收标准按现行标准执行并留存记录。"
    # 用**非数字**差异制造互不相同的骨架（数字会被骨架归一成占位符而合并成一组）
    words = ["甲", "乙", "丙", "丁", "戊", "己", "庚"]
    secs: list[dict] = []
    for pair, w in enumerate(words):
        shared = (f"专项条款{w}：基坑支护采用钻孔灌注桩，桩径九百毫米，"
                  f"混凝土强度 C30，施工前进行图纸会审与技术交底。")
        for k in (pair * 2, pair * 2 + 1):
            body = "\n\n".join([shared, filler,
                                f"第 {k} 章独有内容 {k} 如下所述，逐条落实到位。"])
            secs.append({"id": f"s{k}", "title": f"第{k}章 专项", "content": body,
                         "word_count": len(body), "parent_id": None})
    return secs


def _ctx() -> pf.PreflightContext:
    return pf.PreflightContext(scheme_id="r90", scheme_name="护栏方案",
                               scheme_type="decoration", word_budget=1500,
                               sections=_dup_sections(), charts=[], facts=[])


@pytest.fixture
def truncated_copies(monkeypatch):
    """把跨章搬运检测强制打到「截断」形态（pairwise 预算置 0）。

    ⚠️ 必须 monkeypatch **duplicate_detection 模块常量**：`preflight_engine`
    以 `from ... import find_cross_section_copies` 绑定函数，函数内部读的是
    所在模块的全局量，改本模块常量才生效（改 preflight_engine 的名字无效）。
    """
    monkeypatch.setattr(dd, "MAX_PAIRWISE_COMPARISONS", 0)
    res = dd.find_cross_section_copies(_dup_sections(), exclude_pairs=[])
    assert res.get("truncated") is True, "护栏前置条件失配：未触发截断，用例空转"
    return res


# ===========================================================================
# A 组 · BUG-RV-1：_finding 严重度形参 + 自造编号（2 处独立缺陷）
# ===========================================================================
class TestFindingSeverityKwarg:
    def test_severity_is_an_accepted_kwarg(self):
        """根因①：`_finding` 必须接受 severity —— 否则调用点直接 TypeError。"""
        assert "severity" in inspect.signature(pf._finding).parameters

    def test_severity_is_keyword_only_with_none_default(self):
        """加法式契约：必须是 keyword-only 且缺省 None，否则既有调用点会漂移。"""
        p = inspect.signature(pf._finding).parameters["severity"]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is None

    def test_default_none_falls_back_to_registry_severity(self):
        """缺省（None）→ 取注册表值，与本函数引入前**逐字一致**。"""
        rule = ar.get_rule("CMP-01")
        assert pf._finding("CMP-01", "x")["severity"] == rule.severity
        assert pf._finding("CMP-01", "x", severity=None)["severity"] == rule.severity

    def test_explicit_override_wins(self):
        assert pf._finding("CMP-01", "x", severity="low")["severity"] == "low"

    def test_override_also_works_when_rule_unresolvable(self):
        """未登记编号（理论上不该再出现）走兜底分支时 override 仍生效。"""
        f = pf._finding("ZZ-99", "x", severity="low")
        assert f["severity"] == "low"
        assert f["dimension"] == ""      # 兜底语义保持
        assert f["title"] == "ZZ-99"

    @pytest.mark.parametrize("sev", sorted(SEVERITY_ORDER))
    def test_override_values_are_in_the_registry_domain(self, sev):
        assert pf._finding("CMP-01", "x", severity=sev)["severity"] == sev

    def test_default_behaviour_unchanged_for_every_existing_call_shape(self):
        """回归锁：既有关键字组合下逐字不变（sse_handlers 的护栏依赖此性质）。"""
        base = pf._finding("CMP-02", "d")
        rich = pf._finding("CMP-02", "d", evidence=["a", "b"], section_id="s1",
                           section_title="t", suggestion="sg")
        assert set(base) == set(rich)
        assert rich["severity"] == base["severity"] == "block"
        assert rich["evidence"] == ["a", "b"]
        assert rich["section_id"] == "s1" and rich["suggestion"] == "sg"


class TestNoSelfInventedRuleId:
    """根因②：`_finding` 的 rule_id 必须能被 `_resolve_base_rule` 解析。

    `CON-06-TRUNC` 这类「基编号 + 非数字后缀」无法被解析（`_resolve_base_rule`
    只剥离 `-<数字>`），会让 dimension 退化为空串 → `unknown_dimension_count`
    被污染、`title` 退化成裸串、`basis` 为空 —— 与 `CON-05-N` 同款病。
    """

    def test_invented_id_is_proof_of_the_defect_class(self):
        """前提自证：这类编号确实解析不了（否则本组用例是空转的）。"""
        assert ar.get_rule("CON-06-TRUNC") is None
        assert pf._resolve_rule("CON-06-TRUNC") is None

    def test_every_literal_rule_id_passed_to_finding_must_resolve(self):
        """AST 通检：**任何**传给 `_finding` 的字面量 rule_id 都必须可解析。

        比「点名 CON-06-TRUNC」强：日后任何人再造一个 `-TRUNC` / `-XXX` 编号
        都会红，且不需要人记得这号前缀。
        """
        tree = ast.parse(_ENGINE_PY.read_text(encoding="utf-8"))
        bad: list[str] = []
        checked = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_finding"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue  # 动态编号（CON-06-{idx}）单独由「派生形态」用例覆盖
            rid = node.args[0].value
            if not isinstance(rid, str) or not re.match(r"^[A-Z]{3}-", rid):
                continue
            checked += 1
            if pf._resolve_rule(rid) is None:
                bad.append(rid)
        assert checked >= 10, f"扫描器失灵：只查到 {checked} 个字面量编号"
        assert bad == [], f"以下编号无法解析（dimension/basis/title 会退化）：{bad}"

    def test_derived_ids_still_resolve(self):
        assert pf._resolve_rule("CON-06-1") is not None
        assert pf._resolve_rule("CON-05-12") is not None

    def test_registry_and_emitted_set_stay_healthy(self):
        assert validate_rule_registry(
            emitted_rule_ids=ar._PROGRAM_EMITTED_RULE_IDS) == []


class TestTruncationBranchDoesNotKillChecker:
    """BUG-RV-1 的行为层：截断形态下 CON-06 结论**不得**整体丢失。"""

    def test_checker_does_not_raise_when_truncated(self, truncated_copies):
        out = pf.check_duplication(_ctx())          # 修复前在此抛 TypeError
        assert out, "截断提示必须可见（旧实现设计意图）"

    def test_real_copy_groups_are_not_lost(self, truncated_copies):
        baseline = {f["rule_id"] for f in pf.check_duplication(_ctx())
                    if f["rule_id"].startswith("CON-06-")}
        truncated = {f["rule_id"] for f in pf.check_duplication(_ctx())
                     if f["rule_id"].startswith("CON-06-")}
        assert baseline, "前提自证：语料本应产生真实搬运组"
        assert truncated == baseline, "真实成段雷同结论被截断分支吞掉"

    def test_truncation_notice_has_registered_dimension_and_basis(self, truncated_copies):
        notice = [f for f in pf.check_duplication(_ctx())
                  if f["rule_id"] == "CON-06"]
        assert len(notice) == 1
        rule = ar.get_rule("CON-06")
        assert notice[0]["dimension"] == rule.dimension
        assert notice[0]["basis"] == rule.basis
        assert notice[0]["title"] == rule.title

    def test_truncation_notice_is_low_severity(self, truncated_copies):
        notice = [f for f in pf.check_duplication(_ctx())
                  if f["rule_id"] == "CON-06"][0]
        assert notice["severity"] == "low", "元信息按 medium 报会虚增扣分"

    def test_truncation_notice_carries_actionable_suggestion(self, truncated_copies):
        notice = [f for f in pf.check_duplication(_ctx())
                  if f["rule_id"] == "CON-06"][0]
        assert len(notice["suggestion"]) >= 10, "修复建议必须可执行，不是空串"

    def test_run_preflight_emits_no_engine_error(self, truncated_copies):
        allf = pf.run_preflight(_ctx())
        assert [f for f in allf if f.get("engine_error")] == []

    def test_unknown_dimension_stays_zero(self, truncated_copies):
        from app.services import audit_scoring as sc
        allf = pf.run_preflight(_ctx())
        assert sc.score_findings(allf).unknown_dimension_count == 0

    def test_truncation_does_not_inflate_the_consistency_score(self, truncated_copies):
        """回归锁根因最恶劣的一面：检查器崩溃曾让 consistency **涨到 100**。"""
        from app.services import audit_scoring as sc
        scores = {}
        for label, cap in (("ok", 60000), ("truncated", 0)):
            dd.MAX_PAIRWISE_COMPARISONS = cap
            try:
                res = sc.score_findings(pf.run_preflight(_ctx()))
                scores[label] = {d.key: d.score for d in res.dimensions}["consistency"]
            finally:
                dd.MAX_PAIRWISE_COMPARISONS = 60000
        assert scores["truncated"] <= scores["ok"], (
            "截断形态的 consistency 分数不得高于正常形态（说明搬运结论被吞了）")


# ===========================================================================
# B 组 · BUG-CG-1：正文终态顺序（checkpoint → finish_task → yield）
# ===========================================================================
_TERMINAL_EVENTS = ("completed", "stopped", "error", "degraded")


class TestContentTerminalOrderingAllPaths:
    """**全量式**终态顺序不变量（AST 级，替代逐路径点名式断言）。

    为什么不用逐路径抄锚点：空章节路径的载荷是内联字典字面量，与其它三条
    路径的 `_terminal_status` / `_stopped_payload(...)` 锚点全都不同 ⇒ 逐路径
    写法必然漏掉它（这正是缺陷存活的直接原因）。

    ⚠️ 判据口径（AGENTS §5.14「判据与风险同构」）：
    真正的风险是「**同一条语句列表**里，终态 yield 之后还有 finish_task」
    —— 那几行在客户端断开后是死代码。而 ``finally`` 里的兜底 finish_task
    **合法地**位于终态 yield 之后（它属于另一个语句列表 `Try.finalbody`），
    故必须按「同一语句列表」判定，不能全局比较行号。
    """

    def _fn_ast(self):
        tree = ast.parse(inspect.getsource(sh.generate_content))
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "generate_content"):
                return node
        raise AssertionError("未定位到 generate_content")

    @staticmethod
    def _is_terminal_yield(node: ast.stmt, src: str) -> bool:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Yield):
            return False
        seg = ast.get_source_segment(src, node) or ""
        if "data:" not in seg:
            return False
        return any(ev in seg for ev in _TERMINAL_EVENTS)

    @staticmethod
    def _finish_lines(node: ast.AST) -> set[int]:
        out: set[int] = set()
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "finish_task"):
                out.add(n.lineno)
        return out

    def _scan(self) -> list[tuple[str, int, int]]:
        """返回 [(所在语句列表, yield 行号, 其后同列表内的 finish_task 行号)]。"""
        fn = self._fn_ast()
        src = inspect.getsource(sh.generate_content)
        violations: list[tuple[str, int, int]] = []
        found = 0
        for body_owner in ast.walk(fn):
            for field in ("body", "orelse", "finalbody"):
                lst = getattr(body_owner, field, None)
                if not isinstance(lst, list) or not lst:
                    continue
                for i, st in enumerate(lst):
                    if not self._is_terminal_yield(st, src):
                        continue
                    found += 1
                    later = {ln for st2 in lst[i + 1:] for ln in self._finish_lines(st2)}
                    for ln in sorted(later):
                        violations.append(
                            (type(body_owner).__name__, st.lineno, ln))
        self._found = found
        return violations

    def test_scanner_is_not_vacuous(self):
        """前提自证：必须至少认出 4 条终态 yield，否则整组用例空转
        （AGENTS §5.14：阳性断言前先确认判据真的能判）。"""
        self._scan()
        assert self._found >= 4, f"扫描器只认出 {self._found} 条终态 yield"

    def test_no_finish_task_after_a_terminal_yield_in_same_block(self):
        self._scan()
        assert self._found >= 4
        assert self._scan() == [], (
            "终态 yield 之后仍有同一语句列表内的 finish_task —— 该行会被 "
            "GeneratorExit 跳过，任务终态将被 finally 兜底改写为 stopped")

    def test_finally_cleanup_is_still_allowed(self):
        """反向锁：确认判据没有过严到把合法的 finally 兜底也判红。

        没有这一条，一个「把所有 finish_task 全删掉」的 mutant 也能让上一条
        变绿 —— 护栏必须既能抓缺陷、又不奖励破坏实现。
        """
        src = inspect.getsource(sh.generate_content)
        finally_body = src.split("finally:")[-1]
        assert "finish_task" in finally_body, "断线兜底分支被摘除，实现已残缺"

    def test_empty_leaves_path_finishes_before_yield(self):
        """点名复锁修复点本身（防止「全量式」将来被放宽成空转）。"""
        src = self._src()
        i_finish = src.index('await finish_task(task_id, "completed")')
        i_yield = src.index("'没有待生成的章节'", i_finish)
        assert i_finish < i_yield, "空章节路径必须先 finish_task 再 yield"

    def test_empty_leaves_still_writes_checkpoint_first(self):
        src = self._src()
        i_ckpt = src.index('_content_ckpt_payload("completed", "没有待生成的章节")')
        i_finish = src.index('await finish_task(task_id, "completed")', i_ckpt)
        assert i_ckpt < i_finish, "顺序必须为 checkpoint → finish_task → yield"

    def _src(self) -> str:
        return inspect.getsource(sh.generate_content)


class TestContentEmptyLeavesTerminalStatus:
    async def test_zero_leaf_task_ends_completed_not_stopped(self, monkeypatch, tmp_path):
        """行为实证（零 AI 调用、零生产库写入）：空章节任务的终态必须是 completed。

        复现手法（沿 AGENTS §4.14 既有配方）：复制运行库到 tmp → 重定向
        `app.db.DB_PATH` → 桩掉 AI 调用 → 走**真实** SSE 流。

        ⚠️ 必须用 ``await conn.execute(...)``：**不能**碰 ``conn._conn``
        （aiosqlite 的 sqlite 连接活在它自己的工作线程里，跨线程直调抛
        ``ProgrammingError``）—— 本条护栏首版即踩此坑。
        """
        import json as _json
        import shutil
        import app.db as _appdb
        from app.services.ai import task_registry as tr

        src_db = pathlib.Path(_appdb.DB_PATH)
        for suffix in ("", "-wal", "-shm"):
            s = pathlib.Path(str(src_db) + suffix)
            if s.exists():
                shutil.copy2(s, tmp_path / s.name)
        prev_path = _appdb.DB_PATH
        _appdb.DB_PATH = tmp_path / src_db.name
        await _appdb.init_db()

        conn = await _appdb.get_conn()
        try:
            cur = await conn.execute("SELECT id FROM schemes LIMIT 1")
            row = await cur.fetchone() if cur is not None else None
            sid = row[0] if row else ""
            assert sid, "运行库无方案，无法构造空章节场景"
            # ⚠️ 必须让 **sections 表对该方案彻底为空**（而不是「正文为空」）：
            #   首版只清 content，结果 `select_target_leaves` 仍返回全部叶子
            #   → 走进完整生成链路（AI 桩抛错 → 重试退避 6s×2）→ 本用例
            #   跑了 201s 才失败。零章节才是 `if not leaves:` 早退分支。
            await conn.execute("DELETE FROM sections WHERE scheme_id=?", (sid,))
            await conn.commit()

            async def _boom(*a, **k):   # 绝不该被调用
                raise AssertionError("空章节早退路径不应触发任何 AI 调用")

            monkeypatch.setattr(sh, "chat_with_fallback", _boom, raising=False)
            monkeypatch.setattr(sh, "collect_json_response", _boom, raising=False)

            events: list[dict] = []
            # ⚠️ `generate_content` 是**普通 async 函数**（返回
            #    StreamingResponse），真正的异步生成器是内部闭包
            #    `event_stream()` —— 直接 `sh.generate_content(...)` 拿到的是
            #    coroutine，没有 `aclose()`。本条护栏首版即踩此坑。
            resp = await sh.generate_content(sid, _FakeReq(), db=conn)
            stream = resp.body_iterator
            try:
                async for chunk in stream:
                    m = re.search(r"data:\s*(\{.*\})", chunk or "")
                    if m:
                        events.append(_json.loads(m.group(1)))
                    # 模拟前端：收到终态事件立即断开（break + aclose）
                    if events and events[-1].get("event") in (
                            "completed", "stopped", "error"):
                        break
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()

            assert events, "未收到任何 SSE 事件"
            assert events[-1]["event"] == "completed", (
                f"终态事件应为 completed，实得 {events[-1]['event']}")
            tid = events[-1].get("task_id") or ""
            assert tid not in tr._tasks, (
                "任务已 finish_task 清理，不应仍留在 _tasks（否则 finally 会改写）")
        finally:
            await _appdb.close_db()
            _appdb.DB_PATH = prev_path


class _FakeReq:
    """最小 Request 替身（只需要 body 字典）。"""

    def __init__(self, body: dict | None = None):
        self.body = body or {}
        self.headers: dict = {}
        self.method = "POST"
        self.url = "/api/v1/sse/generate-content"


# ===========================================================================
# C 组 · BUG-RV-2：导出预检来源不得静默消失
# ===========================================================================
class _NoneCursorDB:
    """代理连接：任何 ``execute`` 都返回 None（R13 形态），commit 照常成功。"""

    def __init__(self):
        self.exec_calls = 0

    async def execute(self, *a, **k):
        self.exec_calls += 1
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class TestExportIssueReadStrictness:
    def test_strict_read_is_additive_keyword_default_false(self):
        p = inspect.signature(ex.collect_export_issues).parameters
        assert "strict_read" in p
        assert p["strict_read"].default is False

    async def test_default_stays_fail_soft(self):
        """导出页行为必须逐字不变（默认不抛）。"""
        out = await ex.collect_export_issues("s1", _NoneCursorDB())
        assert isinstance(out, dict) and "issues" in out

    async def test_strict_read_raises_on_db_failure(self):
        with pytest.raises(ex.DBReadError):
            await ex.collect_export_issues("s1", _NoneCursorDB(), strict_read=True)

    def test_db_fetch_one_gained_strict_param(self):
        assert "strict" in inspect.signature(ex._db_fetch_one).parameters

    def test_every_internal_read_passes_strict_read_through(self):
        """全部查询出口必须透传 —— 漏一路则该路仍可静默消失。

        ⚠️ 必须走 **AST**：本条护栏首版用正则扫源码文本，结果被
        ``collect_export_issues`` docstring 里那句「四路查询原本一律
        ``_db_fetch_all(strict=False)``」**自咬**（AGENTS §5.14「源码含字符串
        在自己写的注释里会自咬」同族）。AST 只认真实调用节点，docstring 是
        ``Constant``，结构上不可能触发。

        ⚠️ 第二版又把 ``n.func`` 判成 ``ast.Attribute`` —— 而
        ``_db_fetch_all(...)`` 是**裸名调用**（``ast.Name``），判据自己失灵
        静默返回 0 个调用点（§5.14 同族：「判据失灵」比「判据错」更危险）。
        故下方同时断言「至少 4 个调用点」作为扫描器自证。
        """
        tree = ast.parse(inspect.getsource(ex.collect_export_issues))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in ("_db_fetch_all", "_db_fetch_one")]
        assert len(calls) >= 4, f"查询出口数不足（{len(calls)}），请同步本护栏"
        for n in calls:
            kw = {k.arg for k in n.keywords}
            assert "strict" in kw, (
                f"{n.func.id} 调用未传 strict（第 {n.lineno} 行）")


class TestReadinessOverviewSeesExportSourceFailure:
    def test_total_inspection_requests_strict_read(self):
        """⚠️ 必须走 **AST**（A/B M5 实测教训）。

        本条护栏首版断言 ``"strict_read=True" in inspect.getsource(...)``，
        A/B 把调用点的实参删掉后**依然全绿** —— 因为本轮修复在调用点上方写
        的说明注释里就含 ``strict_read=True`` 这七个字（AGENTS §5.14「源码含
        字符串在自己写的注释里会自咬」，本轮已第三次踩到同族）。文本判据对
        「实参是否真的传了」零判别力，AST 关键字节点才认。
        """
        from app.routers import compliance as cmp_mod
        tree = ast.parse(inspect.getsource(cmp_mod._readiness_overview_compute))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "collect_export_issues"]
        assert len(calls) == 1, f"总检侧 collect_export_issues 调用点数异常：{len(calls)}"
        kw = {k.arg: k.value for k in calls[0].keywords}
        assert "strict_read" in kw, (
            "总检侧必须传 strict_read=True，否则 failed_sources 对 DB 读失败不可达")
        assert isinstance(kw["strict_read"], ast.Constant) and \
            kw["strict_read"].value is True

    def test_export_page_call_site_keeps_default(self):
        """导出页**不得**改成严格读（否则 DB 抖动会让导出预检 500）。

        同样走 AST：反向文本判据会被注释里的同名串误伤（判据过严比过宽更糟，
        它会逼着后人把护栏改松）。
        """
        tree = ast.parse(inspect.getsource(ex.export_check))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "collect_export_issues"]
        assert len(calls) == 1, f"导出页调用点数异常：{len(calls)}"
        assert not calls[0].keywords, (
            "导出页必须沿用 fail-soft 缺省（不得传 strict_read=True）")


# ===========================================================================
# D 组 · BUG-RV-4：repair_record 纳入单一 DB 出口 + 写路径不再假成功
# ===========================================================================
class TestRepairRecordSingleExit:
    def test_no_bare_db_execute_left(self):
        """静态锁：模块内不得再出现裸 `db.execute`（R78 已把四个 router 收口）。"""
        src = _REPAIR_RECORD_PY.read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "db"):
                offenders.append(node.lineno)
        assert offenders == [], f"仍存在裸 db.execute（行号）：{offenders}"

    def test_module_imports_the_single_exit(self):
        src = _REPAIR_RECORD_PY.read_text(encoding="utf-8")
        assert "from app.services.review_db import" in src

    async def test_mark_repair_status_raises_instead_of_false_success(self):
        from app.services import repair_record as rr
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await rr.mark_repair_status(_NoneCursorDB(), "R_1", "confirmed")
        assert ei.value.status_code == 503

    async def test_get_snapshot_raises_503_not_none(self):
        """DB 不可用时不得谎报「快照不存在」（空结果会被当业务答案）。"""
        from app.services import repair_record as rr
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await rr.get_snapshot(_NoneCursorDB(), "ver_x")
        assert ei.value.status_code == 503

    async def test_get_repair_raises_503(self):
        from app.services import repair_record as rr
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await rr.get_repair(_NoneCursorDB(), "R_1")
        assert ei.value.status_code == 503

    async def test_list_repairs_raises_503(self):
        from app.services import repair_record as rr
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await rr.list_repairs(_NoneCursorDB(), "s1")
        assert ei.value.status_code == 503

    async def test_save_repair_insert_verified(self):
        from app.services import repair_record as rr
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await rr.save_repair(rr.db if False else _NoneCursorDB(),
                                 scheme_id="s1", scan_id="c1", mode="m",
                                 items=[], snapshot_id="v1", total_conflicts=0)

    async def test_create_snapshot_insert_verified(self):
        from app.services import repair_record as rr
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await rr.create_snapshot(_NoneCursorDB(), "s1",
                                     [{"section_id": "a", "content_before": "x"}])

    def test_callers_do_not_swallow_the_503_into_success(self):
        """反向锁：调用点不得用 `except Exception` 把 503 吞成成功体。"""
        for rel in ("app/routers/consistency_repair.py", "app/routers/review_autofix.py"):
            src = (_BACKEND / rel).read_text(encoding="utf-8")
            for m in re.finditer(r"mark_repair_status\(", src):
                window = src[max(0, m.start() - 260):m.start() + 120]
                assert "except Exception" not in window, (
                    f"{rel}: mark_repair_status 附近存在裸 except，503 会被吞成假成功")


# ===========================================================================
# E 组 · 触碰文件字节体检（BOM / U+FFFD / 孤立 CR / NUL / 行尾混用）
# ===========================================================================
_TOUCHED = [
    "app/services/preflight_engine.py",
    "app/services/repair_record.py",
    "app/routers/export.py",
    "app/routers/compliance.py",
    "app/routers/sse_handlers.py",
]


class TestByteHealth:
    @pytest.mark.parametrize("rel", _TOUCHED)
    def test_no_bom_no_replacement_char_no_lone_cr(self, rel):
        raw = (_BACKEND / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "写入 BOM"
        assert raw.count(b"\xef\xbf\xbd") == 0, "存在 U+FFFD（中文已损坏）"
        lone = len(re.findall(rb"\r(?!\n)", raw))
        assert lone == 0, f"存在 {lone} 处孤立 CR（\r\r\n 损坏形态）"

    @pytest.mark.parametrize("rel", _TOUCHED)
    def test_no_mixed_line_endings(self, rel):
        raw = (_BACKEND / rel).read_bytes()
        crlf = len(re.findall(rb"\r\n", raw))
        bare = len(re.findall(rb"(?<!\r)\n", raw))
        assert not (crlf and bare), f"行尾混用：CRLF={crlf} bareLF={bare}"

    @pytest.mark.parametrize("rel", _TOUCHED)
    def test_parses_as_python(self, rel):
        ast.parse((_BACKEND / rel).read_text(encoding="utf-8"))

    @pytest.mark.parametrize("rel", _TOUCHED)
    def test_no_control_chars(self, rel):
        raw = (_BACKEND / rel).read_bytes()
        assert not re.search(rb"[\x00\x07]", raw), "存在 NUL/BEL"