# -*- coding: utf-8 -*-
"""R115 · 目录生成模块收口护栏（零 AI 确定性兜底 + 跳过可归因 · 2026-10-20）

用户症状（原话）：「目录（TOC）暴露出明显问题：三级标题大量是占位式泛化名称
（如『专项6』『总体实施策划（2）』『总体实施策划（3）』），且存在编号冲突后缀。」

## 为什么 R114 之后症状仍会复发

R114 修的是「落库上下文 / 逐章闸门 / 绝不丢章 / 可归因」四件事，判据与上下文
都已就位，但**修复链路的可用性**没修：``_try_outline_title_fix`` 的 5 个出口
里有 4 个在 AI 不可用时返回 ``None``（超时 / 异常 / 首轮无产出 / 全批保守红线
拒收），两条调用方（生成期 ``_review_and_fix_outline``、落库期
``autofix_generic_titles_on_save``）一律 fail-soft 原样保留 ⇒ 占位标题原样进
sections 表、进 DOCX/PDF。

生产日志实证（trace=acdef1e40a64，2026-10-20 15:06:26~15:11:06，scheme=室外
汽车吊吊装施工专项方案）：
  · agnes 持续 ``HTTP 401 该令牌状态不可用``、zhipu 429、sensetime 超时；
  · 15:10:06 ``外科式补齐超时（120s），回退完整 AI 审核``；
  · 15:11:06 ``目录审核超时（60s），跳过审核直接完成``；
  · 请求以 **280582ms** 返回 200。
⇒ AI 链路整体不可用是**常态**，不能让它成为占位标题的唯一防线。

## 本轮修复

  · **FIX-1** 零 AI 确定性兜底 ``deterministic_title_fallback``：用节点自身的
    标题基名 / ``description`` 派生候选，仍走 ``_apply_title_updates`` 同一套
    六条保守红线；``_try_outline_title_fix`` 全部出口收敛到 ``_finalize``。
    并覆盖「AI 只改掉一部分」这条真实形态（``ai-applied`` 出口也补兜底）。
  · **FIX-2** ``_skip_ai`` 快路径：上游已实证 AI 不可用（补齐超时 / 审核超时）
    时不再重复等一个 ``OUTLINE_FIX_TIMEOUT``。
  · **FIX-3** 审核 / 修复「跳过」不再谎报为「通过」：加法式键
    ``review_skipped`` / ``review_skip_reason`` / ``fix_skipped``。
  · **FIX-4** 新配置项 ``outline_deterministic_title_fallback``（默认 True；
    False 即逐字回到本项引入前）。

护栏分组：
  A. 判据单一事实源（``strip_trailing_counter_suffix``）
  B. 候选派生（整段 / 剥引导语 / 分句 / 标点收口截断 / 空 description）
  C. 兜底行为（改写成功 / 不编造 / 结构指纹 / 纯函数 / 配置门控 / 红线仍生效）
  D. ``_try_outline_title_fix`` 出口收敛（_skip_ai / 超时 / 异常 / 无产出 /
     全批被拒 / **部分成功仍补兜底** / 配置 False 逐字回退）
  E. 落库链路继承（``autofix_generic_titles_on_save``）
  F. FIX-2 / FIX-3 接线与可观测性（AST 判据，注释不算）

运行：pytest tests/test_r115_outline_deterministic_fallback_20261020.py -q
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from app.config import settings
from app.services import outline_title_fix as otf
from app.services.outline_quality import (
    detect_generic_titles,
    detect_generic_titles_for_write,
    strip_trailing_counter_suffix,
)

_BACKEND = Path(__file__).resolve().parents[1]
_SRC_SSE = _BACKEND / "app" / "routers" / "sse_handlers.py"
_SRC_OTF = _BACKEND / "app" / "services" / "outline_title_fix.py"


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------
def _tree() -> list:
    """用户症状的三种形态 + 一个干净节点（必须逐字保留）。"""
    return [
        {"title": "工程概况", "children": [
            {"title": "项目简介", "description": "工程名称、地点、结构形式及参建责任主体"},
            {"title": "专项6", "description": "编制依据及适用范围说明"},
            {"title": "总体实施策划（2）", "description": "施工总体部署、进度计划与资源配置"},
        ]},
        {"title": "施工部署", "children": [
            {"title": "总体实施策划（3）", "description": ""},
            {"title": "专项12", "description": "本节主要介绍起重吊装作业的安全技术措施要点"},
        ]},
    ]


def _run(coro):
    return asyncio.run(coro)


def _fixed_titles(tree: list) -> dict:
    out = {}

    def _walk(nodes, prefix=""):
        for i, n in enumerate(nodes, 1):
            path = f"{prefix}.{i}" if prefix else str(i)
            out[path] = n.get("title")
            kids = n.get("children")
            if isinstance(kids, list):
                _walk(kids, path)

    _walk(tree)
    return out


def _dict_keys_in_func(src_path: Path, func_name: str) -> set[str]:
    """收集某函数体内所有 dict 字面量的 str 键，以及所有 Subscript 的 str 键。

    ✅ 用 AST 而不是 ``in source`` 文本匹配：注释里出现同名键名会造成**假绿**
    （本仓 R111 教训：静态文本锁的替换注释不得含原关键字）。
    """
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for k in sub.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
                elif isinstance(sub, ast.Subscript):
                    sl = sub.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        keys.add(sl.value)
    return keys


def _func_kwarg_names(src_path: Path, func_name: str) -> set[str]:
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            a = node.args
            names = {x.arg for x in list(a.args) + list(a.kwonlyargs)}
            return names
    return set()


def _own_returns(fn_node) -> list:
    """收集函数**自身**的 return（不进入嵌套函数体）。

    ``ast.walk(fn)`` 会把 ``_finalize`` / ``_fallback_pass`` 等闭包里的 return
    一并算进来（本仓 R111 教训：``ast.walk`` 扫函数体必须先剥嵌套作用域）。
    """
    out = []
    stack = list(fn_node.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Return):
            out.append(n)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _calls_with_kwarg(src_path: Path, callee: str, kwarg: str) -> int:
    """统计 ``callee(...=kwarg ...)`` 在源码中出现的次数（AST 判据）。"""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != callee:
            continue
        if any(k.arg == kwarg for k in node.keywords):
            n += 1
    return n


# ---------------------------------------------------------------------------
# A. 判据单一事实源
# ---------------------------------------------------------------------------
class TestACounterSuffixFactSource:
    @pytest.mark.parametrize("raw,expected", [
        ("总体实施策划（2）", "总体实施策划"),
        ("监测措施(3)", "监测措施"),
        ("措施（三）", "措施"),
        ("专项6", "专项6"),            # 无后缀 ⇒ 原样（不得吃掉数字）
        ("深基坑支护", "深基坑支护"),
        ("标高（1.5m）", "标高（1.5m）"),   # 非纯序号 ⇒ 不剥（防误伤部位限定）
    ])
    def test_strip_shapes(self, raw, expected):
        assert strip_trailing_counter_suffix(raw) == expected

    def test_strip_removes_invisible_tail(self):
        # 零宽字符族先剥，后缀才可能命中（与 has_trailing_counter_suffix 同源）
        assert strip_trailing_counter_suffix("监测措施（2）\u200b") == "监测措施"

    def test_not_a_second_regex_copy(self):
        """必须复用 outline_quality 的正则，不得在 outline_title_fix 里另写一份。"""
        src = _SRC_OTF.read_text(encoding="utf-8")
        # 允许「（N）」出现在注释里，但模块级不得出现自造的后缀正则
        assert "_TRAILING_COUNTER_SUFFIX_RE = " not in src
        assert "strip_trailing_counter_suffix" in src


# ---------------------------------------------------------------------------
# B. 候选派生
# ---------------------------------------------------------------------------
class TestBCandidates:
    def test_whole_description_first(self):
        c = otf._desc_title_candidates("施工总体部署、进度计划与资源配置")
        assert c[0] == "施工总体部署、进度计划与资源配置"

    def test_strips_leading_noise(self):
        c = otf._desc_title_candidates("本节主要介绍起重吊装作业的安全技术措施要点")
        assert c and c[0] == "起重吊装作业的安全技术措施要点"
        assert not any(x.startswith("本节") for x in c)

    def test_sentence_split_when_too_long(self):
        c = otf._desc_title_candidates(
            "工程名称、地点、结构形式、维修面积及参建责任主体、开工与竣工日期等")
        assert c
        assert all(otf._FALLBACK_TITLE_MIN_LEN <= len(x) <= otf._FALLBACK_TITLE_MAX_LEN
                   for x in c)

    def test_truncate_closes_at_punct(self):
        # 无句读可切（只有「、」在截断位之前）⇒ 截断并去掉悬空连接词
        c = otf._desc_title_candidates(
            "工程名称、地点、结构形式、维修面积及参建责任主体开工竣工日期说明")
        assert c
        assert not c[0].endswith(("及", "和", "与", "、"))

    def test_empty_description_yields_nothing(self):
        assert otf._desc_title_candidates("") == []
        assert otf._desc_title_candidates(None) == []
        assert otf._desc_title_candidates("本节介绍") == []

    def test_base_name_only_when_suffix_removed(self):
        base_suffix = {"title": "总体实施策划（2）", "description": ""}
        base_plain = {"title": "专项6", "description": ""}
        assert otf._fallback_title_candidates(base_suffix) == ["总体实施策划"]
        # 无后缀可剥 ⇒ 不把原标题当候选（否则白跑一轮红线）
        assert otf._fallback_title_candidates(base_plain) == []


# ---------------------------------------------------------------------------
# C. 兜底行为
# ---------------------------------------------------------------------------
class TestCDeterministicFallback:
    def test_user_symptom_titles_are_rewritten(self):
        tree = _tree()
        hits = detect_generic_titles(tree)
        # 注意「总体实施策划（3）」同样命中（``^总体实施策划`` 前缀），共 4 处
        assert {h["title"] for h in hits} == {
            "专项6", "总体实施策划（2）", "总体实施策划（3）", "专项12"}
        fixed = otf.deterministic_title_fallback(tree, hits)
        assert fixed is not None
        got = _fixed_titles(fixed)
        assert got["1.2"] == "编制依据及适用范围说明"
        assert got["1.3"] == "施工总体部署、进度计划与资源配置"
        assert got["2.2"] == "起重吊装作业的安全技术措施要点"
        # 干净节点逐字不变
        assert got["1.1"] == "项目简介"
        assert got["1"] == "工程概况"

    def test_no_description_is_kept_not_invented(self):
        """无 description 的占位标题**不得编造**（宁可不改）。"""
        tree = _tree()
        hits = detect_generic_titles(tree)
        fixed = otf.deterministic_title_fallback(tree, hits) or tree
        assert _fixed_titles(fixed)["2.1"] == "总体实施策划（3）"

    def test_structure_fingerprint_unchanged(self):
        tree = _tree()
        fixed = otf.deterministic_title_fallback(tree, detect_generic_titles(tree))
        assert otf._tree_shape(tree) == otf._tree_shape(fixed)

    def test_pure_function(self):
        tree = _tree()
        snapshot = json.dumps(tree, ensure_ascii=False)
        otf.deterministic_title_fallback(tree, detect_generic_titles(tree))
        assert json.dumps(tree, ensure_ascii=False) == snapshot

    def test_config_off_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "outline_deterministic_title_fallback", False)
        tree = _tree()
        assert otf.deterministic_title_fallback(tree, detect_generic_titles(tree)) is None

    def test_redline_still_applies_same_level_clash(self):
        """候选与同级标题重名 ⇒ 红线照旧拒收（兜底不得绕过 _apply_title_updates）。"""
        tree = [{"title": "章", "children": [
            {"title": "编制依据及适用范围说明", "description": ""},
            {"title": "专项6", "description": "编制依据及适用范围说明"},
        ]}]
        hits = detect_generic_titles(tree)
        fixed = otf.deterministic_title_fallback(tree, hits)
        assert fixed is None      # 唯一候选被红线③拒收 ⇒ 不生效
        assert tree[0]["children"][1]["title"] == "专项6"

    def test_redline_still_applies_counter_suffix(self):
        """候选带（N）后缀 ⇒ 红线⑥拒收。"""
        tree = [{"title": "章", "children": [
            {"title": "专项6", "description": "总体实施策划（2）的具体内容"},
        ]}]
        fixed = otf.deterministic_title_fallback(tree, detect_generic_titles(tree))
        assert fixed is None

    def test_no_hits_returns_none(self):
        assert otf.deterministic_title_fallback([{"title": "工程概况"}], []) is None


# ---------------------------------------------------------------------------
# D. _try_outline_title_fix 出口收敛
# ---------------------------------------------------------------------------
def _patch_ai(monkeypatch, handler):
    """把模块级 collect/render 换成替身；返回调用计数。"""
    calls = {"collect": 0, "render": 0}

    async def _collect(messages, validate_fn=None, **kwargs):
        calls["collect"] += 1
        return await handler(calls["collect"])

    def _render(name, **kwargs):
        calls["render"] += 1
        return "PROMPT"

    monkeypatch.setattr(otf, "collect_json_response", _collect)
    monkeypatch.setattr(otf, "render", _render)
    return calls


class TestDFixExits:
    def test_skip_ai_makes_zero_ai_calls(self, monkeypatch):
        calls = _patch_ai(monkeypatch, lambda n: (_ for _ in ()).throw(
            AssertionError("_skip_ai 时不得发起 AI 调用")))
        tree = _tree()
        out = _run(otf._try_outline_title_fix(
            tree, detect_generic_titles(tree), scheme_name="探针", _skip_ai=True))
        assert calls["collect"] == 0 and calls["render"] == 0
        assert out is not None
        # 2.1「总体实施策划（3）」无 description ⇒ 不得编造，如实保留
        assert _fixed_titles(out)["2.1"] == "总体实施策划（3）"
        assert {"专项6", "总体实施策划（2）", "专项12"} & set(
            _fixed_titles(out).values()) == set()

    def test_timeout_still_returns_fallback(self, monkeypatch):
        async def _boom(n):
            raise asyncio.TimeoutError()
        _patch_ai(monkeypatch, _boom)
        tree = _tree()
        out = _run(otf._try_outline_title_fix(tree, detect_generic_titles(tree)))
        assert out is not None
        assert "专项6" not in _fixed_titles(out).values()

    def test_error_still_returns_fallback(self, monkeypatch):
        async def _boom(n):
            raise RuntimeError("provider down")
        _patch_ai(monkeypatch, _boom)
        tree = _tree()
        out = _run(otf._try_outline_title_fix(tree, detect_generic_titles(tree)))
        assert out is not None
        assert "专项6" not in _fixed_titles(out).values()

    def test_ai_empty_still_returns_fallback(self, monkeypatch):
        async def _empty(n):
            return {}, None
        _patch_ai(monkeypatch, _empty)
        tree = _tree()
        out = _run(otf._try_outline_title_fix(tree, detect_generic_titles(tree)))
        assert out is not None
        assert "专项6" not in _fixed_titles(out).values()

    def test_all_rejected_still_returns_fallback(self, monkeypatch):
        async def _still_generic(n):
            return {"updates": [{"path": "1.2", "title": "专项7"}]}, None
        _patch_ai(monkeypatch, _still_generic)
        tree = _tree()
        out = _run(otf._try_outline_title_fix(tree, detect_generic_titles(tree)))
        assert out is not None
        assert "专项6" not in _fixed_titles(out).values()

    def test_partial_ai_success_fallback_covers_residual(self, monkeypatch):
        """AI 只改掉 1 条时，**剩余残留也必须被兜底**（真实「大量」形态）。"""
        seq = [{"updates": [{"path": "1.2", "title": "编制依据与适用范围"}]},
               {"updates": []}]

        async def _partial(n):
            return seq[min(n, len(seq)) - 1], None
        _patch_ai(monkeypatch, _partial)
        tree = _tree()
        out = _run(otf._try_outline_title_fix(tree, detect_generic_titles(tree)))
        assert out is not None
        got = _fixed_titles(out)
        assert got["1.2"] == "编制依据与适用范围"        # AI 的成果被保留
        assert got["1.3"] != "总体实施策划（2）"          # 残留由兜底接手
        assert got["2.2"] != "专项12"

    def test_config_off_is_verbatim_rollback(self, monkeypatch):
        async def _boom(n):
            raise asyncio.TimeoutError()
        _patch_ai(monkeypatch, _boom)
        monkeypatch.setattr(settings, "outline_deterministic_title_fallback", False)
        tree = _tree()
        assert _run(otf._try_outline_title_fix(
            tree, detect_generic_titles(tree))) is None

    def test_all_exits_go_through_finalize(self):
        """源码锁：`_try_outline_title_fix` 内不得再有裸 `return None` 失败出口。"""
        src = _SRC_OTF.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_try_outline_title_fix")
        returns = _own_returns(fn)
        bare_none = [r for r in returns
                     if isinstance(r.value, ast.Constant) and r.value.value is None]
        # 允许的两处：配置关闭 / path 全不存在（兜底同样无效）
        assert len(bare_none) == 2, [getattr(r, "lineno", 0) for r in bare_none]
        # 其余出口必须走 _finalize（6 处：applied / empty / timeout / error /
        # all-rejected / skipped）
        names = [getattr(r.value, "func", None) for r in returns]
        assert sum(1 for x in names
                   if isinstance(x, ast.Name) and x.id == "_finalize") == 6


# ---------------------------------------------------------------------------
# E. 落库链路继承（autofix_generic_titles_on_save）
# ---------------------------------------------------------------------------
class TestESaveChainInherits:
    def test_autofix_on_save_uses_fallback_when_ai_down(self, monkeypatch):
        async def _boom(n):
            raise RuntimeError("AI down")
        _patch_ai(monkeypatch, _boom)
        tree = _tree()
        hits = detect_generic_titles(tree)
        fixed, left, count = _run(otf.autofix_generic_titles_on_save(
            tree, hits, scheme_name="探针", context="ut"))
        # 4 处检出里 3 处可确定性改写（2.1 无 description ⇒ 如实保留）
        assert count == 3, (count, left)
        assert [h["title"] for h in left] == ["总体实施策划（3）"]
        assert {"专项6", "总体实施策划（2）", "专项12"} & set(
            _fixed_titles(fixed).values()) == set()

    def test_autofix_on_save_config_off_keeps_original(self, monkeypatch):
        async def _boom(n):
            raise RuntimeError("AI down")
        _patch_ai(monkeypatch, _boom)
        monkeypatch.setattr(settings, "outline_deterministic_title_fallback", False)
        tree = _tree()
        hits = detect_generic_titles(tree)
        fixed, left, count = _run(otf.autofix_generic_titles_on_save(tree, hits))
        assert count == 0 and len(left) == len(hits)
        assert _fixed_titles(fixed)["1.2"] == "专项6"

    async def test_ai_down_fallback_lands_in_sections_table(
            self, db_conn, monkeypatch):
        """端到端数据链：AI 全挂 ⇒ 兜底标题**真的落进 sections 表**。

        与上一条的区别：上一条验内存级返回，本条验**数据结构贯通**。
        依据本仓纪律（R114 教训①）：「确认已落地」必须落在行为而非文字 ——
        上一条即使通过，也只证明「函数返回了新树」，不证明调用方真的写库。
        """
        from app.routers import sections as sec

        await db_conn.execute(
            "INSERT INTO projects (id, name, engineering_type) VALUES (?,?,?)",
            ("p115", "某某大厦室外吊装工程", "起重吊装工程"))
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name, type, config_json)"
            " VALUES (?,?,?,?,?)",
            ("s115", "p115", "室外汽车吊吊装施工专项方案", "起重吊装工程",
             json.dumps({}, ensure_ascii=False)))
        await db_conn.commit()

        async def _ai_down(*a, **kw):
            # 复刻生产实证（trace=acdef1e40a64）：agnes HTTP 401，整条 AI 链路不可用
            raise RuntimeError("Provider agnes 调用失败: HTTP 401 该令牌状态不可用")

        monkeypatch.setattr(otf, "collect_json_response", _ai_down)
        monkeypatch.setattr(settings, "outline_autofix_generic_on_save", True,
                            raising=False)

        result = await sec._save_outline_to_db(
            db_conn, "s115",
            [{"title": "工程概况", "children": [
                {"title": "专项6", "description": "编制依据及适用范围说明",
                 "children": []}]},
             {"title": "施工部署", "children": [
                {"title": "总体实施策划（2）",
                 "description": "施工总体部署、进度计划与资源配置",
                 "children": []}]}],
            source="e2e-r115")

        cur = await db_conn.execute(
            "SELECT title FROM sections WHERE scheme_id=? ORDER BY sort_order",
            ("s115",))
        titles = [r[0] for r in await cur.fetchall()]
        # ① 占位名一个都不许进库
        assert "专项6" not in titles, titles
        assert "总体实施策划（2）" not in titles, titles
        # ② description 派生的具体标题真的落库（不是只在内存里改过）
        assert "编制依据及适用范围说明" in titles, titles
        assert "施工总体部署、进度计划与资源配置" in titles, titles
        # ③ 返回契约不变（ok/count/tree 齐备，AI 失败不阻断保存）
        assert result["ok"] is True
        assert result["count"] == len(titles)
        assert isinstance(result["tree"], list)

    def test_autofix_signature_unchanged(self):
        """向后兼容锁：既有 10 个 kwarg 一个都不能少、顺序不得变。"""
        import inspect
        params = list(inspect.signature(
            otf.autofix_generic_titles_on_save).parameters)
        assert params == [
            "outline", "hits", "scheme_name", "scheme_type", "context",
            "construction_scope", "project_brief", "project_facts",
            "scheme_basis", "standards_text", "requirements"]


# ---------------------------------------------------------------------------
# F. FIX-2 / FIX-3 接线与可观测性
# ---------------------------------------------------------------------------
class TestFWiring:
    def test_patch_accepts_state_outparam(self):
        assert "_state" in _func_kwarg_names(_SRC_SSE, "_try_outline_patch")

    def test_patch_records_failure_reason(self):
        keys = _dict_keys_in_func(_SRC_SSE, "_try_outline_patch")
        assert {"timed_out", "errored"} <= keys

    def test_review_skip_keys_are_additive(self):
        keys = _dict_keys_in_func(_SRC_SSE, "_review_and_fix_outline")
        assert {"review_skipped", "review_skip_reason", "fix_skipped"} <= keys

    def test_skip_ai_wired_at_both_sites(self):
        # 补齐失败 → 标题重写；审核跳过 → 标题重写
        assert _calls_with_kwarg(_SRC_SSE, "_try_outline_title_fix", "_skip_ai") >= 2

    def test_title_fix_accepts_skip_ai_kwonly(self):
        import inspect
        params = inspect.signature(otf._try_outline_title_fix).parameters
        assert "_skip_ai" in params
        assert params["_skip_ai"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["_skip_ai"].default is False

    def test_review_skipped_not_claimed_as_passed_origin(self):
        """审核跳过时不得再写出「审核超时已跳过」+ passed=True 而**无**归因键。

        行为锁：超时分支必须同时设置 ``_review_skip_reason``（源码赋值锁）。
        """
        src = _SRC_SSE.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_review_and_fix_outline")
        assigned = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        assigned.add(t.id)
        assert "_review_skip_reason" in assigned


# ---------------------------------------------------------------------------
# G. 默认值与向后兼容
# ---------------------------------------------------------------------------
class TestGDefaults:
    def test_config_default_true(self):
        assert settings.outline_deterministic_title_fallback is True

    def test_config_field_declared_once(self):
        src = (_BACKEND / "app" / "config.py").read_text(encoding="utf-8")
        assert src.count("outline_deterministic_title_fallback: bool = True") == 1

    def test_module_exports_documented(self):
        """入口函数 docstring 必须声明兜底出口（防后人当作「已有 AI 就够」删掉）。"""
        assert "deterministic_title_fallback" in (
            otf._try_outline_title_fix.__doc__ or "")
        assert "零 AI" in (otf.deterministic_title_fallback.__doc__ or "")


# ---------------------------------------------------------------------------
# H. 候选质量门槛（本轮由既有护栏反向发现）
# ---------------------------------------------------------------------------
class TestHCandidateQualityGate:
    """候选必须是「像中文施工标题」的串 —— 否则兜底会把 description 垃圾值
    搬成三级标题，**比原占位名更糟**。

    发现过程（务必保留）：首版兜底零门槛，``pytest -k outline`` 立刻打红 10 例
    既有护栏 —— 桩里的 ``desc1``（0 个中文）与 ``占位一级``/``占位二级`` 被当成
    合格候选，把「第1章」「专项6」改成了更无意义的名字。这不是「测试桩太假」：
    ``description`` 在生产里确实是模型写的自由文本，「N/A」「见图3」
    「待补充」「占位」都是真实可能值。
    """

    @pytest.mark.parametrize("bad", [
        "desc1", "Figure 3", "N/A", "--", "略", "无", "暂无", "待补充",
        "占位一级", "占位二级", "详见第3章", "同上", "P-01", "图3",
    ])
    def test_junk_rejected(self, bad):
        assert not otf._is_usable_title(bad), bad

    @pytest.mark.parametrize("good", [
        "施工总体部署、进度计划与资源配置",
        "起重吊装作业的安全技术措施要点",
        "无缝钢管支架安装",          # 「无」是单字黑名单，只能整串比对
        "无支护放坡开挖",            # 同上
        "基坑支护施工策略",          # 「略」是单字黑名单，同上
        "工程名称、地点、结构形式及参建责任主体",
    ])
    def test_real_titles_accepted(self, good):
        assert otf._is_usable_title(good), good

    def test_junk_description_yields_no_candidate(self):
        """垃圾 description 连候选都产不出 ⇒ 该节点如实保留（不猜）。"""
        assert otf._fallback_title_candidates(
            {"title": "专项6", "description": "占位一级"}) == []
        assert otf._fallback_title_candidates(
            {"title": "第1章", "description": "desc1"}) == []
        # 「（N）」基名仍可作候选（CJK 6 字，过门槛）
        assert otf._fallback_title_candidates(
            {"title": "总体实施策划（2）", "description": "占位二级"}) == ["总体实施策划"]

    def test_symptom_titles_still_fixed_under_gate(self):
        """门槛不得误伤用户症状的三种形态（真实 description）。"""
        tree = _tree()
        hits = detect_generic_titles(tree)
        out = otf.deterministic_title_fallback(tree, hits)
        assert out is not None
        titles = set(_fixed_titles(out).values())
        assert {"专项6", "总体实施策划（2）", "专项12"} & titles == set()

    def test_gate_constants_are_not_shared_with_judgement(self):
        """质量门槛**不得**下沉到 outline_quality（那边是判定，会被误用）。"""
        src = (_BACKEND / "app" / "services" / "outline_quality.py").read_text(
            encoding="utf-8")
        assert "_is_usable_title" not in src
        assert "_DESC_JUNK_SUBSTR" not in src
