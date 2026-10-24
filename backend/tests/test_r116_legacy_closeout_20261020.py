"""R116 · 遗留问题与后续建议收口（L1 / L2 / L3 + 后续建议 2 与 3）。

本轮不改动 R115 的兜底语义，只把 R115 §九登记的遗留项按**最优方案**落地：

  · **L1 → A 组**：``outline_check_generic_titles``（检不检）× 
    ``outline_deterministic_title_fallback``（兜不兜底）**4 组合矩阵**，锁
    「两开关独立可叠加」+「兜底不得绕过总闸」（关掉检出时 hits 必为空，
    兜底再开也无从动作）。
  · **后续建议 2 → B 组**：兜底**成功侧计数**日志（``落地 N 条（零 AI 调用，
    M 处待改 → K 条候选）``）静态锁 + 行为取证 —— 该日志 R115 已落地，本轮
    补锁**防回流**（缺成功侧计数时，运维只能看到 WARNING，无法判断 AI 健康度）。
  · **L2 → C 组**：**刻意不落地**「多级语义候选」这一"增强"。护栏锁住三条
    边界，防止后人把「宁可产不出候选，也不产出坏标题」改成激进派生：
    ① 候选只来自**节点自身**（不借父/兄弟/项目事实）；② 候选预算有界
    （<= 1 + ``_FALLBACK_DESC_CANDIDATES``）；③ 派生链路**零 AI**。
  · **L3 → D 组**：审核超时的**最优解不是调大默认值**，而是 A-2 的「上游已
    实证不可用 ⇒ 直接跳过」，故本组静态锁 ``outline_review_timeout`` 默认值
    **未被调大** + ``_patch_state`` 必须定义在**函数级**（补齐分支内重新定义会
    切断「补齐超时 → 审核跳过」这条跨阶段信号）+ ``upstream_dead`` 闸门必须
    出现在唯一的审核调用**之前**。

判据纪律：静态锁一律用 AST（注释不算）；行为锁一律走真实函数调用。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
from pathlib import Path

import pytest
from app.config import settings
from app.routers import sse_handlers as sh
from app.services import outline_title_fix as otf
from app.services.outline_quality import detect_generic_titles

_BACKEND = Path(__file__).resolve().parents[1]
_SRC_OTF = _BACKEND / "app" / "services" / "outline_title_fix.py"


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


def _tree() -> list:
    """一棵含 1 处占位标题（``专项6``）且 description 可用的目录树。"""
    return [
        {"title": "工程概况", "children": []},
        {"title": "施工部署", "children": [
            {"title": "专项6", "description": "灌注桩成孔与钢筋笼吊装",
             "children": []}]},
    ]


def _child_titles(nodes: list) -> list:
    """二级标题（测试树里占位标题落在二级，便于一眼看出是否被改写）。"""
    return [c.get("title") for n in (nodes or [])
            for c in (n.get("children") or [])]


async def _boom(*_a, **_k):
    raise asyncio.TimeoutError()


def _src_otf() -> str:
    return _SRC_OTF.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A. L1 —— 两开关 4 组合矩阵（检出 × 兜底）
# ---------------------------------------------------------------------------
class TestATwoSwitchMatrix:
    """``outline_check_generic_titles`` × ``outline_deterministic_title_fallback``。

    为什么值得单独立组：两个开关**语义正交但代码上串联**（检出产出 hits，
    兜底消费 hits），任何一侧被写成「独立生效」都会制造出第三种行为。
    4 组合逐一锁定，等价于把「两开关可叠加」写成可执行契约。
    """

    @pytest.mark.parametrize("check,fallback", [
        (True, True), (True, False), (False, True), (False, False)])
    def test_matrix(self, monkeypatch, check, fallback):
        monkeypatch.setattr(settings, "outline_check_generic_titles", check,
                            raising=False)
        monkeypatch.setattr(settings, "outline_deterministic_title_fallback",
                            fallback, raising=False)
        tree = _tree()
        before = [n["title"] for n in tree[1]["children"]]

        hits = sh._detect_generic_titles(tree)
        # 检出维度：只由 outline_check_generic_titles 决定
        assert bool(hits) is check, (check, fallback, hits)

        got = _run(otf._try_outline_title_fix(
            tree, hits, _collect=_boom, _render=lambda *a, **k: "P"))

        # 兜底维度：必须「检出了」且「兜底开着」才可能落地
        if check and fallback:
            assert got is not None, "两开关全开：AI 超时后必须由确定性兜底接管"
            assert _child_titles(got) == ["灌注桩成孔与钢筋笼吊装"]
        else:
            assert got is None, (check, fallback, "未检出/未开兜底时不得改动目录")
            # 纯函数契约：入参不得被就地改写
            assert [n["title"] for n in tree[1]["children"]] == before

    def test_fallback_never_bypasses_master_switch(self, monkeypatch):
        """兜底开着但总闸关：**零候选**→ 即使 description 完全可用也不动。

        这是 L1 里最容易写错的一条：有人会为了"提高兜底命中率"在
        ``deterministic_title_fallback`` 内部自己再检一遍泛化标题，
        那会让 ``outline_check_generic_titles=False`` 的总闸语义失效。
        """
        monkeypatch.setattr(settings, "outline_check_generic_titles", False,
                            raising=False)
        monkeypatch.setattr(settings, "outline_deterministic_title_fallback", True,
                            raising=False)
        tree = _tree()
        hits = sh._detect_generic_titles(tree)
        assert hits == []
        assert otf.deterministic_title_fallback(tree, hits) is None
        assert _child_titles(tree) == ["专项6"]

    def test_direct_call_respects_fallback_switch(self, monkeypatch):
        """兜底开关在**函数自身**生效，而不是只靠调用方的提前返回。

        为什么单独立一条：``_try_outline_title_fix._finalize`` 在"转兜底"之前
        已有一道同款提前返回 ⇒ 只从该入口观察时，「摘掉函数内门控」是**等价
        变异**（R115 A/B 已记录同族，得到假 NOT-CAUGHT）。而
        ``sections`` / ``upload_outline`` 两条落库链路是**直接调**本函数的，
        它们没有那道提前返回 —— 函数自身必须守约。
        """
        monkeypatch.setattr(settings, "outline_check_generic_titles", True,
                            raising=False)
        monkeypatch.setattr(settings, "outline_deterministic_title_fallback", False,
                            raising=False)
        tree = _tree()
        hits = detect_generic_titles(tree)
        assert hits, "前提：树里确有占位标题"
        assert otf.deterministic_title_fallback(tree, hits) is None
        assert _child_titles(tree) == ["专项6"]

    def test_signature_has_no_hits_bypass(self):
        """AST：``deterministic_title_fallback`` 不得自带「重新检出」入口。

        ``hits`` 是**唯一**输入契约；若函数内部再调 ``detect_generic_titles*``，
        总闸（``outline_check_generic_titles``）就被绕过了。
        """
        fn = _func_ast(otf.deterministic_title_fallback)
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "detect_generic_titles" not in called
        assert "detect_generic_titles_for_write" not in called


# ---------------------------------------------------------------------------
# B. 后续建议 2 —— 兜底成功侧计数（静态锁 + 行为取证）
# ---------------------------------------------------------------------------
class TestBSuccessSideCounter:
    """缺成功侧计数时，运维只能看到 WARNING，无法判断 AI 链路健康度。"""

    _LITERAL = "占位标题确定性兜底落地 %d 条（零 AI 调用，%d 处待改 → %d 条候选）"

    def test_log_literal_declared_once(self):
        src = _src_otf()
        assert src.count(self._LITERAL) == 1, "成功侧计数日志必须恰好一处（防重复/防删）"

    def test_log_literal_lives_in_fallback(self):
        fn = _func_ast(otf.deterministic_title_fallback)
        consts = [n.value for n in ast.walk(fn)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert self._LITERAL in consts, "计数日志必须落在兜底函数体内（不得挪到别处）"

    def test_counter_emitted_with_real_numbers(self, caplog):
        caplog.set_level(logging.INFO, logger="outline_title_fix")
        tree = _tree()
        hits = detect_generic_titles(tree)
        got = otf.deterministic_title_fallback(tree, hits)
        assert got is not None
        msgs = [r.getMessage() for r in caplog.records
                if r.name == "outline_title_fix"]
        assert any("占位标题确定性兜底落地 1 条" in m and "1 处待改" in m
                   for m in msgs), msgs

    def test_counter_silent_when_nothing_landed(self, caplog):
        """未落地时**不得**打「落地」日志（日志准确性：说的就是做的）。"""
        caplog.set_level(logging.INFO, logger="outline_title_fix")
        tree = [{"title": "章", "children": [
            {"title": "专项6", "description": "N/A", "children": []}]}]
        hits = detect_generic_titles(tree)
        assert otf.deterministic_title_fallback(tree, hits) is None
        msgs = [r.getMessage() for r in caplog.records
                if r.name == "outline_title_fix"]
        assert not any("确定性兜底落地" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# C. L2 —— 「多级语义候选」**刻意不落地**的边界护栏
# ---------------------------------------------------------------------------
class TestCL2DeliberateNonLanding:
    """R115 §九 L2：候选只取 description 首分句时，遇「本章依据…」仍可能被拒。

    结论是**不做**多级语义候选：兜底的价值在于「零 AI、零失败面、绝不改坏」，
    引入更激进的派生会让「比原占位名更糟」的标题进库（红线只能判"是不是
    泛化/重名/过长"，判不了"语义是否更差"）。本组把该决策锁成三条可执行边界。
    """

    def test_candidates_only_from_own_node(self):
        """AST：候选只读 ``node['title']`` / ``node['description']``。

        禁止读 ``children`` / 父节点 / 兄弟节点 / ``hits`` / 项目事实 ——
        「借邻居的标题」在三级目录里必然张冠李戴（结构指纹只保证形态不变）。
        """
        fn = _func_ast(otf._fallback_title_candidates)
        assert [a.arg for a in fn.args.args] == ["node"], "签名只允许一个入参 node"
        consts = {n.value for n in _walk_body(fn)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert consts <= {"title", "description", ""}, consts

    def test_candidate_budget_bounded(self):
        """候选预算有界：最多 1（（N）基名）+ ``_FALLBACK_DESC_CANDIDATES``。"""
        assert otf._FALLBACK_DESC_CANDIDATES == 3, "预算常量被放大即等于放开多级候选"
        node = {"title": "专项6（2）",
                "description": "甲；乙；丙；丁；戊；己；庚；辛"}
        cands = otf._fallback_title_candidates(node)
        assert len(cands) <= 1 + otf._FALLBACK_DESC_CANDIDATES, cands

    def test_derivation_chain_is_zero_ai(self):
        """三个派生函数体内不得出现任何 AI 调用（零 AI 是兜底的全部价值）。"""
        banned = {"collect_json_response", "collect_json_response_with_chat",
                  "chat_with_fallback", "render"}
        for fn in (otf.deterministic_title_fallback,
                   otf._fallback_title_candidates,
                   otf._desc_title_candidates):
            body = _func_ast(fn)
            names = set()
            for n in ast.walk(body):
                if isinstance(n, ast.Name):
                    names.add(n.id)
                elif isinstance(n, ast.Attribute):
                    names.add(n.attr)
            assert not (names & banned), (fn.__name__, names & banned)

    def test_decision_documented_in_code(self):
        """决策必须写在**函数体内可见处**，否则下一个人只会看到「为什么不试第二分句」。"""
        doc = otf._desc_title_candidates.__doc__ or ""
        assert "多级" in doc and "刻意" in doc, "L2 不落地决策未在 docstring 里留痕"


# ---------------------------------------------------------------------------
# D. L3 —— 审核入口闸门（A-2）的结构与默认值
# ---------------------------------------------------------------------------
class TestDReviewGate:
    def test_timeout_default_not_inflated(self):
        """最优解不是调大超时：AI 全挂时调大只会让用户等更久。"""
        assert settings.outline_review_timeout == 60
        assert settings.outline_fix_timeout == 120

    def test_gate_config_default_on(self):
        assert settings.outline_skip_review_when_upstream_dead is True

    def test_gate_config_declared_once(self):
        src = (_BACKEND / "app" / "config.py").read_text(encoding="utf-8")
        assert src.count(
            "outline_skip_review_when_upstream_dead: bool = True") == 1

    def test_patch_state_is_function_level(self):
        """``_patch_state`` 只能在函数级定义一次。

        R115 把它定义在补齐分支内（12 空格）；审核调用在函数体顶层（4 空格），
        拿不到该信号 ⇒ 补齐已 120s 超时证明链路不可用，审核仍要再白等 60s。
        """
        fn = _func_ast(sh._review_and_fix_outline)
        defs = [(n.lineno, n.col_offset)
                for n in ast.walk(fn)
                if isinstance(n, (ast.Assign, ast.AnnAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                if isinstance(t, ast.Name) and t.id == "_patch_state"]
        assert len(defs) == 1, defs
        assert defs[0][1] == 4, f"_patch_state 必须在函数体顶层定义，实际缩进 {defs[0][1]}"

    def test_upstream_dead_gate_precedes_review_call(self):
        fn = _func_ast(sh._review_and_fix_outline)
        gate = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Name) and t.id == "_upstream_dead"]
        assert len(gate) == 1, gate
        review_calls = [
            n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
            for kw in n.keywords
            if kw.arg == "scene" and isinstance(kw.value, ast.Constant)
            and kw.value.value == "outline_review"]
        assert len(review_calls) == 1, "审核调用必须恰好一处（否则有绕过闸门的分支）"
        assert gate[0] < review_calls[0], "闸门必须出现在审核调用之前"

    def test_review_skip_reason_is_upstream_dead(self):
        src = inspect.getsource(sh._review_and_fix_outline)
        assert '_review_skip_reason = "upstream_dead"' in src


# ---------------------------------------------------------------------------
# F. A-4 —— 四处目录提示词的 description 内容契约
# ---------------------------------------------------------------------------
class TestFDescriptionContract:
    """后续建议 3（上游治理）：description 必须是**具体施工内容短语**。

    为什么必须四处齐改：短方案直出（``outline_short_system``）、长方案一级
    （``outline_level1_system``）、单章子目录（``outline_sublevel_system``）与
    批量子目录（``outline_sublevel_batch_system``）是四条独立渲染路径，漏掉
    任一条，该路径产出的 description 就仍会退化成「本章依据…」这类空话，
    进而让兜底候选被红线全批拒收（R115 L2 的真实上游）。
    """

    _KEYS = (
        "outline_short_system",
        "outline_level1_system",
        "outline_sublevel_system",
        "outline_sublevel_batch_system",
    )

    @staticmethod
    def _text(key: str) -> str:
        import app.services.ai.prompts.outline  # noqa: F401 - 确保完成注册
        from app.services.ai.prompts._registry import get_default_prompt
        text = get_default_prompt(key)
        assert text, f"{key} 未注册或默认内容为空"
        return text

    @pytest.mark.parametrize("key", _KEYS)
    def test_requires_concrete_construction_phrase(self, key):
        text = self._text(key)
        assert "具体施工内容短语" in text, key
        assert "8~20 字" in text, key
        assert "兜底候选" in text, f"{key}: 必须说明「为何要写具体」"

    @pytest.mark.parametrize("key", _KEYS)
    def test_bans_meta_language_and_placeholders(self, key):
        text = self._text(key)
        for banned in ("本章依据", "详见", "同上", "待补充", "N/A", "占位标记"):
            assert banned in text, (key, banned)

    def test_all_four_sites_covered(self):
        """四条渲染路径必须**都**带该契约（漏一条即该路径仍有缺口）。"""
        missing = [k for k in self._KEYS
                   if "8~20 字" not in self._text(k)]
        assert missing == [], missing


# ---------------------------------------------------------------------------
# E. 前后端契约闭环（后端新增归因值 ⇒ 前端必须消费）
# ---------------------------------------------------------------------------
class TestEFrontendParity:
    """后端写 ``review_skip_reason="upstream_dead"``，前端不映射 = 用户看到英文 token。"""

    _PAGE = _BACKEND.parent / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"

    def test_frontend_reason_map_covers_upstream_dead(self):
        src = self._PAGE.read_text(encoding="utf-8")
        assert "upstream_dead:" in src, "前端 reasonMap 缺 upstream_dead 映射"
        assert "本章目录补齐已失败" in src, "映射文案必须点明「同一链路已失败」"

    def test_backend_literal_matches_frontend_key(self):
        """值必须逐字一致（大小写/下划线不同即前端取不到映射）。"""
        assert '_review_skip_reason = "upstream_dead"' in inspect.getsource(
            sh._review_and_fix_outline)
        assert "upstream_dead" in self._PAGE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _func_ast(func) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise AssertionError(f"未取到函数体: {getattr(func, '__name__', func)}")


def _walk_body(fn: ast.FunctionDef) -> list:
    """遍历函数体并**剥掉 docstring**（首节点 Constant 会污染字符串判据）。"""
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    out = []
    for stmt in body:
        out.extend(ast.walk(stmt))
    return out
