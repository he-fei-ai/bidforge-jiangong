# -*- coding: utf-8 -*-
"""R178（2026-11-05）后台日志审计遗留项收口护栏。

本文件锁定 R178 落地的 7 项修复，全部来自 R177 报告登记的「剩余未修项遗留
问题与后续建议」，并以 ``logs/backend.log``（2026-10-28 ~ 2026-11-05，
39,276 行 / 4.8 MB）的生产实证为动因：

A 组 · D-1 内置兜底豁免判死（provider_factory）
    生产实证：内置兜底 agnes 被设计注释明写「兜底必须留在链内」，但它在
    「成功率过滤之后」追加、随后又流经 ``_order_candidates`` 的判死剔除 ⇒
    一旦兜底自身成功率过低即被**静默剔除**，而这恰恰发生在主配置也已不可用
    的最坏时刻 ⇒ 兜底形同虚设。

B 组 · D-2 三处「设计内节流」日志由 WARNING 降 INFO
    生产实证：「已剔除实时成功率过低的候选」372 条 + 「主配置…后置」642 条 +
    「fallback 候选…已自动排除」253 条 = 1014 条 WARNING，占全量 WARNING 的
    24%，把真正的故障（429/401/锁冲突/库损坏）彻底淹没。按 R103 先例降 INFO，
    并以反向棘轮锁住「旧 WARNING 形态不得回流」。

C 组 · D-3 保存配置必须清实时成功率统计
    生产实证：此前只清「确定性死连击」，不清实时成功率 ⇒ 用户把模型名/端点/
    密钥改对了，成功率却仍停在旧值（判死阈值 0.20 / 后置阈值 0.60）→ 改对了
    仍被剔除或静默后置；且统计一旦冻结就**永远判死**，唯一出口是进程重启。

D 组 · D-4 content_plan 输出预算地板
    生产实证：8 天内 158 条「离散编排阶段 LLM 调用失败，回退为无编排」——
    编排阶段事实上长期处于关闭状态，而用户毫不知情。根因是硬编码
    ``max_tokens=600``（对推理模型必然被思考过程吃光）。

E 组 · B-4 预检计算移入工作线程（compliance）
    预检是纯 CPU 同步计算（8 个 checker + CON-01 的 11 条数值正则 + 整章查重），
    此前在事件循环内直跑 ⇒ 一次预检把整个事件循环卡住（SSE 心跳/其它请求全被拖）。

F 组 · F-13 4xx/5xx 失败原因入访问日志（middleware）
    生产实证：14 条 400 的访问日志里**零条**能看出失败原因（原因只存在于
    响应体，服务端访问日志只记 status/cost）⇒ 400 完全不可反查。

G 组 · B-9 PDF 导出 Word COM 进程级串行（export）
    Word COM 是单例且不可重入，两个并发 PDF 导出会各自 Dispatch 一个 Word
    实例争抢同一 COM 服务器 ⇒ 实测间歇性 0x800706BE（RPC 失败）与文档损坏。

手法：行为实证（内存库 / monkeypatch / 线程）+ AST 源码锁（防后人误删）
      + 反向棘轮（旧 WARNING 形态不得回流）。
"""

from __future__ import annotations

import ast
import logging
import pathlib
import threading
import time

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
PF_PATH = APP_DIR / "services" / "ai" / "provider_factory.py"
CP_PATH = APP_DIR / "services" / "content_plan.py"
CO_PATH = APP_DIR / "routers" / "compliance.py"
MW_PATH = APP_DIR / "middleware.py"
EX_PATH = APP_DIR / "routers" / "export.py"
SH_PATH = APP_DIR / "routers" / "sse_handlers.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _logger_calls_with_text(path: pathlib.Path, needle: str) -> list[tuple[str, int, str]]:
    """找出源码中所有「首参文本含 needle」的 ``logger.<级别>(...)`` 调用。

    返回 ``(级别名, 行号, 参数字面量拼接文本)`` 列表。
    """
    src = _src(path)
    tree = ast.parse(src)
    hits: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in {"info", "warning", "error", "debug", "exception"}):
            continue
        if not (isinstance(f.value, ast.Name) and f.value.id == "logger"):
            continue
        text = "".join((ast.get_source_segment(src, a) or "") for a in node.args)
        if needle in text:
            hits.append((f.attr, node.lineno, text))
    return hits


def _seed_reliability(pf, provider: str, model: str = "m1", ok: int = 0, fail: int = 20) -> str:
    """把某候选身份灌入指定成败样本 ⇒ 实时成功率 = ok/(ok+fail)。"""
    key = pf._reliability_key(provider, model, "", f"http://{provider}")
    with pf._provider_reliability_lock:
        pf._provider_reliability[key] = {"ok": ok, "fail": fail}
    return key


# =====================================================================
# A 组 · D-1 内置兜底豁免判死（行为实证）
# =====================================================================
class TestBuiltinFallbackExemptFromDead:
    """内置兜底必须留在链内：只降级，不剔除。"""

    def test_dead_normal_candidate_is_dropped(self):
        """对照：普通死配置仍必须被剔除（本护栏不放松既有判死口径）。"""
        from app.services.ai import provider_factory as pf

        _seed_reliability(pf, "badco")
        candidates = [
            {"provider_name": "goodco", "model": "m1", "base_url": "http://goodco"},
            {"provider_name": "badco", "model": "m1", "base_url": "http://badco"},
        ]
        names = [c.get("provider_name") for c in pf._order_candidates(candidates)]
        assert "badco" not in names, "普通死配置必须被剔除"
        assert "goodco" in names, "健康候选必须保留"

    def test_builtin_fallback_survives_dead(self):
        """带 ``_is_builtin_fallback`` 标记的候选，即便判死也必须留在链内。"""
        from app.services.ai import provider_factory as pf

        _seed_reliability(pf, "badco")
        candidates = [
            {"provider_name": "goodco", "model": "m1", "base_url": "http://goodco"},
            {
                "provider_name": "badco",
                "model": "m1",
                "base_url": "http://badco",
                "_is_builtin_fallback": True,
            },
        ]
        names = [c.get("provider_name") for c in pf._order_candidates(candidates)]
        assert "badco" in names, "内置兜底必须豁免判死剔除（兜底不得被截断）"
        assert "goodco" in names

    def test_builtin_fallback_flag_present_in_source(self):
        """源码锁：内置兜底构造处必须带豁免标记。"""
        src = _src(PF_PATH)
        assert '"_is_builtin_fallback": True' in src, "内置兜底必须打 _is_builtin_fallback 标记"
        assert 'c.get("_is_builtin_fallback")' in src, "_order_candidates 必须消费该标记做豁免"

    def test_builtin_fallback_marker_not_used_for_user_config(self):
        """豁免只认显式标记，不得按 provider_name 粗判（用户自配 agnes 仍受判死管）。"""
        src = _src(PF_PATH)
        assert 'provider_name") == "agnes"' not in src, "不得按 provider_name 粗判内置兜底"
        assert 'provider_name"] == "agnes"' not in src


# =====================================================================
# B 组 · D-2 三处「设计内节流」日志降 INFO（行为实证 + 反向棘轮）
# =====================================================================
class TestDesignThrottleLogsDowngraded:
    """自愈机制的正常动作不得占用 WARNING 通道。"""

    _NEEDLES = (
        "已剔除实时成功率过低的候选",
        "后置到候选链尾部",
        "已自动排除",
    )

    def test_each_throttle_log_exists_and_is_info(self):
        for needle in self._NEEDLES:
            hits = _logger_calls_with_text(PF_PATH, needle)
            assert len(hits) == 1, f"文案「{needle}」应恰好命中 1 处 logger 调用，实际 {len(hits)}：{hits}"
            level, lineno, _ = hits[0]
            assert level == "info", f"文案「{needle}」（{PF_PATH.name}:{lineno}）必须是 INFO，实际 {level}"

    def test_throttle_logs_never_warning(self):
        """反向棘轮：旧 WARNING 形态不得回流。"""
        for needle in self._NEEDLES:
            for level, lineno, _ in _logger_calls_with_text(PF_PATH, needle):
                assert level != "warning", f"{PF_PATH.name}:{lineno} 的「{needle}」不得回到 WARNING"

    def test_drop_log_emitted_at_info_level(self, caplog):
        """行为实证：触发剔除时，日志记录级别必须是 INFO。"""
        from app.services.ai import provider_factory as pf

        _seed_reliability(pf, "badco")
        candidates = [
            {"provider_name": "goodco", "model": "m1", "base_url": "http://goodco"},
            {"provider_name": "badco", "model": "m1", "base_url": "http://badco"},
        ]
        with caplog.at_level(logging.INFO, logger="provider_factory"):
            pf._order_candidates(candidates)
        recs = [r for r in caplog.records if "已剔除实时成功率过低的候选" in r.getMessage()]
        assert recs, "剔除动作必须留痕（降级为 INFO，不删除）"
        assert all(r.levelno == logging.INFO for r in recs), "剔除日志必须为 INFO"

    def test_demote_log_emitted_at_info_level(self, caplog):
        """行为实证：主配置后置时日志必须为 INFO。"""
        from app.services.ai import provider_factory as pf

        # 成功率 0.25：高于判死阈值 0.20（不会被剔除）、低于后置阈值 0.60 ⇒ 必走后置分支
        _seed_reliability(pf, "primaryco", ok=5, fail=15)
        candidates = [
            {
                "provider_name": "primaryco",
                "model": "m1",
                "base_url": "http://primaryco",
                "_is_primary": True,
            },
            {"provider_name": "goodco", "model": "m1", "base_url": "http://goodco"},
        ]
        with caplog.at_level(logging.INFO, logger="provider_factory"):
            pf._order_candidates(candidates)
        recs = [r for r in caplog.records if "后置到候选链尾部" in r.getMessage()]
        assert recs, "主配置后置必须留痕"
        assert all(r.levelno == logging.INFO for r in recs), "主配置后置日志必须为 INFO"


# =====================================================================
# C 组 · D-3 保存配置清实时成功率（行为实证）
# =====================================================================
class TestSaveConfigClearsReliability:
    """用户改配置 = 立即恢复，不得被冻结的成功率永久判死。"""

    @pytest.mark.asyncio
    async def test_save_clears_reliability_stats(self, db_conn):
        from app.services.ai import provider_factory as pf

        cid = "cid-r178-reliability"
        key = pf._reliability_key("", "", cid, "")
        with pf._provider_reliability_lock:
            pf._provider_reliability[key] = {"ok": 1, "fail": 9}
        assert key in pf._provider_reliability

        await pf.save_ai_config(
            {
                "id": cid,
                "provider_name": "openai",
                "model": "gpt-r178-test",
                "plan": "payg",
                "base_url": "https://api.openai.com/v1",
            }
        )
        assert key not in pf._provider_reliability, "保存配置必须清掉该身份的实时成功率统计"

    @pytest.mark.asyncio
    async def test_save_keeps_other_identity_stats(self, db_conn):
        """只清「本次保存的那条配置」，不得误伤其它配置的统计。"""
        from app.services.ai import provider_factory as pf

        other = pf._reliability_key("", "", "cid-r178-other", "")
        with pf._provider_reliability_lock:
            pf._provider_reliability[other] = {"ok": 3, "fail": 1}
        await pf.save_ai_config(
            {
                "id": "cid-r178-target",
                "provider_name": "openai",
                "model": "gpt-r178-test",
                "plan": "payg",
                "base_url": "https://api.openai.com/v1",
            }
        )
        assert other in pf._provider_reliability, "不得误清其它配置的可靠性统计"


# =====================================================================
# D 组 · D-4 content_plan 输出预算地板
# =====================================================================
class TestContentPlanTokenBudget:
    """编排步不得再被 600 token 的硬编码预算掐死。"""

    def test_floor_constant_value(self):
        from app.services.content_plan import CONTENT_PLAN_MAX_TOKENS

        assert CONTENT_PLAN_MAX_TOKENS >= 1536, "编排步输出预算地板不得低于 1536"

    def test_call_site_uses_constant(self):
        src = _src(CP_PATH)
        assert "max_tokens=CONTENT_PLAN_MAX_TOKENS" in src, "调用点必须引用常量"

    def test_no_hardcoded_small_budget_left(self):
        """源码锁：编排步不得残留任何 <1536 的字面量预算（AST 判据，不受注释干扰）。"""
        tree = ast.parse(_src(CP_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "max_tokens" and isinstance(kw.value, ast.Constant):
                    assert isinstance(kw.value.value, int) and kw.value.value >= 1536, (
                        f"{CP_PATH.name}:{node.lineno} 的 max_tokens 字面量 {kw.value.value} 低于地板 1536"
                    )


# =====================================================================
# E 组 · B-4 预检计算移入工作线程（AST 源码锁）
# =====================================================================
class TestPreflightRunsInWorkerThread:
    """预检是纯 CPU，同步直跑会卡住整个事件循环。"""

    def test_helper_wrapper_exists(self):
        src = _src(CO_PATH)
        assert "def _preflight_findings_blocking(" in src, "必须存在阻塞版 helper"
        assert "await asyncio.to_thread(_preflight_findings_blocking, ctx)" in src, "调用点必须走工作线程"

    def test_run_preflight_only_called_inside_helper(self):
        """``run_preflight`` 只能在 helper 内被调用（由 to_thread 承载）。"""
        tree = ast.parse(_src(CO_PATH))
        direct: list[tuple[str, int]] = []
        helper_calls = 0
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != "run_preflight":
                    continue
                if fn.name == "_preflight_findings_blocking":
                    helper_calls += 1
                else:
                    direct.append((fn.name, node.lineno))
        assert helper_calls == 1, f"helper 内应有且仅有 1 处 run_preflight 调用，实际 {helper_calls}"
        assert not direct, f"事件循环内不得直调 run_preflight，发现：{direct}"

    def test_stats_aggregation_also_offloaded(self):
        src = _src(CO_PATH)
        assert "await asyncio.to_thread(preflight_stats, ctx)" in src, "预检统计聚合同样必须移出事件循环"


# =====================================================================
# F 组 · F-13 4xx/5xx 失败原因入访问日志（行为实证）
# =====================================================================
class _StubApp:
    """最小 ASGI 应用：回一个固定状态码 + 固定响应体。"""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    async def __call__(self, scope, receive, send) -> None:  # noqa: ARG002
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": self.body})


async def _drive(app, path: str = "/api/v1/schemes/x") -> None:
    from app.middleware import RequestLogMiddleware

    mw = RequestLogMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    await mw(scope, receive, send)


class TestErrorReasonInAccessLog:
    """400 的失败原因必须可从服务端日志反查。"""

    @pytest.mark.asyncio
    async def test_error_reason_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger="http"):
            await _drive(_StubApp(400, b'{"detail":"scheme not found"}'))
        recs = [r for r in caplog.records if "失败原因" in r.getMessage()]
        assert recs, "4xx 必须补记失败原因"
        assert "scheme not found" in recs[0].getMessage(), "失败原因必须带上响应体内容"

    @pytest.mark.asyncio
    async def test_success_response_has_no_reason_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="http"):
            await _drive(_StubApp(200, b'{"ok":true}'))
        recs = [r for r in caplog.records if "失败原因" in r.getMessage()]
        assert not recs, "2xx 不得打失败原因日志"

    @pytest.mark.asyncio
    async def test_reason_truncated_to_bounded_length(self, caplog):
        """超长错误体必须截断，不得把整段塞进日志。"""
        long_body = ("E" * 5000).encode()
        with caplog.at_level(logging.INFO, logger="http"):
            await _drive(_StubApp(500, long_body))
        recs = [r for r in caplog.records if "失败原因" in r.getMessage()]
        assert recs, "5xx 必须补记失败原因"
        assert len(recs[0].getMessage()) < 700, "失败原因必须截断（日志长度上限）"

    @pytest.mark.asyncio
    async def test_access_log_format_unchanged(self, caplog):
        """既有访问日志格式不得被破坏（加法式，不改原行）。"""
        with caplog.at_level(logging.INFO, logger="http"):
            await _drive(_StubApp(200, b"{}"))
        access = [r for r in caplog.records if "->" in r.getMessage() and "[trace=" in r.getMessage()]
        assert access, "访问日志必须仍在"
        msg = access[0].getMessage()
        assert "GET /api/v1/schemes/x -> 200 (" in msg, f"访问日志格式被改动：{msg}"


# =====================================================================
# G 组 · B-9 PDF 导出 Word COM 进程级串行（行为实证）
# =====================================================================
class TestPdfConversionSerialized:
    """Word COM 单例 ⇒ 进程级串行（跨线程，必须 threading.Lock）。"""

    def test_concurrent_conversion_never_overlaps(self, monkeypatch):
        from app.routers import export as ex

        state = {"cur": 0, "max": 0}
        guard = threading.Lock()

        def fake_unlocked(docx_path: str, pdf_path: str) -> str:
            with guard:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.05)
            with guard:
                state["cur"] -= 1
            return pdf_path

        monkeypatch.setattr(ex, "_convert_docx_to_pdf_unlocked", fake_unlocked)

        results: list = []

        def worker() -> None:
            try:
                results.append(ex._convert_docx_to_pdf("a.docx", "b.pdf"))
            except Exception as exc:  # noqa: BLE001 - pragma: no cover - 失败即视为未串行
                results.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not any(isinstance(r, Exception) for r in results), f"转换线程异常：{results}"
        assert len(results) == 4, "所有线程都应完成"
        assert state["max"] == 1, f"PDF 转换必须进程级串行，实际最大并发 {state['max']}"

    def test_lock_is_threading_lock_not_asyncio(self):
        """必须跨线程串行 ⇒ threading.Lock；asyncio.Lock 在工作线程里无效。"""
        src = _src(EX_PATH)
        assert "_PDF_CONVERT_LOCK = threading.Lock()" in src, "必须用 threading.Lock（跨线程串行）"
        assert "import threading" in src

    def test_call_site_still_offloaded_to_thread(self):
        """转换仍在工作线程执行（COM 要求调用线程先 CoInitialize）。"""
        src = _src(EX_PATH)
        assert "asyncio.to_thread(_convert_docx_to_pdf" in src, "PDF 转换必须经 asyncio.to_thread 执行"


# =====================================================================
# H 组 · B-1 跨章自检移入工作线程（AST 源码锁 + 锁边界锁）
# =====================================================================
class TestCrossCheckOffloadedToThread:
    """跨章自检是 O(N²) 纯 CPU，必须移出事件循环（判据逐字不变）。

    实测基线（``artifacts/r178/bench_outline_rebuild.py``，章节正文 4000 字）：
        N=10 → 37 ms ｜ N=20 → 83 ms ｜ N=40 → 195 ms（隐含指数 2.36）
    ⇒ N=40 的方案，**每章**生成后阻塞事件循环 195 ms，全书约 7.8 s。
    """

    _TARGETS = ("cross_section_copy_findings", "cross_section_value_findings")

    def _parse(self):
        src = _src(SH_PATH)
        tree = ast.parse(src)
        parents: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        return src, tree, parents

    def _calls(self, tree):
        """定位两个判据函数的**引用点**（``asyncio.to_thread(fn, ...)`` 的 fn 是
        ``ast.Name`` 节点而非 ``ast.Call``，按 Call 找会全部漏判）。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in self._TARGETS:
                yield node

    def test_both_calls_offloaded_to_thread(self):
        """两处跨章判据调用必须包在 ``asyncio.to_thread`` 内。"""
        src, tree, parents = self._parse()
        offloaded = 0
        direct: list = []
        for node in self._calls(tree):
            p = parents.get(node)
            if isinstance(p, ast.Call) and isinstance(p.func, ast.Attribute) and p.func.attr == "to_thread":
                offloaded += 1
            else:
                direct.append((node.id, node.lineno))
        assert offloaded == 2, f"两处跨章判据调用必须移入工作线程，实际 {offloaded}"
        assert not direct, f"跨章判据不得在事件循环内直调，发现：{direct}"

    def test_calls_not_inside_write_lock(self):
        """``await`` 会让出控制权 ⇒ 跨章自检必须在写锁之外，否则破坏互斥。"""
        src, tree, parents = self._parse()
        bad: list = []
        for node in self._calls(tree):
            cur = node
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, ast.AsyncWith):
                    seg = ast.get_source_segment(src, cur) or ""
                    if "_db_write_lock" in seg:
                        bad.append(node.lineno)
        assert not bad, f"跨章自检不得位于写锁内：{bad}"

    def test_criteria_arguments_unchanged(self):
        """判据逐字不变：同一实现、同一参数（只改执行位置，不改判定口径）。"""
        src = _src(SH_PATH)
        assert "cross_section_copy_findings, _dup_secs, new_section_id=section_id" in src
        assert "cross_section_value_findings, _dup_secs, new_section_id=section_id" in src

    def test_fail_soft_semantics_preserved(self):
        """fail-soft 语义不得丢：判据异常只告警、绝不阻断正文落库。"""
        src = _src(SH_PATH)
        assert "跨章搬运检测失败（忽略，不影响落库）" in src
        assert "数值一致性自检失败（忽略，不影响落库）" in src
