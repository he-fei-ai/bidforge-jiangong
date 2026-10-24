# -*- coding: utf-8 -*-
"""R114 · 目录生成模块收口护栏（2026-10-20）

用户症状（原话）：「目录（TOC）暴露出明显问题：三级标题大量是占位式泛化名称
（如『专项6』『总体实施策划（2）』『总体实施策划（3）』），且存在编号冲突后缀。」

本轮根因核查定位到**三条仍能让占位标题落库/成形的链路**（R73/R79/R85/R89/R101
已修过的部分不复述）：

  A. **落库链路上下文缺失**（FIX-A）
     ``autofix_generic_titles_on_save`` 是 stopped/error 部分成果、上传识别、
     目录库套用、AI 调整、手工保存五类入口的**最后一道自动修复机会**，但它内部
     调 ``_try_outline_title_fix`` 时只传 ``scheme_name`` / ``scheme_type`` ——
     提示词里的 ``construction_scope`` / ``scheme_basis`` / ``project_brief`` /
     ``requirements`` 全渲染为空串。模型拿不到「这个专项具体是什么施工」的信息，
     只能回「专项6」「总体实施策划（2）」，再被六条保守红线（仍泛化 / 带（N）
     后缀 / 与同级重名）全批拒收 ⇒ ``applied=0`` ⇒ fail-soft 原样落库。
     与 R89 G3 是**同一个根因**，只是 R89 只修了生成链路。

  B. **生成期无逐章占位闸门**（FIX-B）
     ``_sublevel_validate_fn`` 只判「非空数组 + 每项有 title」——占位名完全合法，
     一路合并进最终目录；唯一拦截点是收尾后的 ``_review_and_fix_outline``
     （单次外科式重写最多 40 条、超时即 fail-soft）。「源头不限 + 末段限额」=
     用户看到的「三级标题**大量**是占位名」。

  C. **停止/全失败成果未经审核且不可归因**（FIX-C）
     ``_finish_stopped_with_partial`` / ``all_failed`` 分支直接返回部分成果，
     收尾审核从未跑过；旧日志对此零留痕，用户与运维都无法判断这批占位名来自
     哪条链路。

护栏分组：
  A. 落库链路上下文（签名默认值 / 装配函数 / 两条调用点透传 / 透传链）
  B. 生成期逐章闸门（检出 / 配置门控 / 首轮脏重试 / 两轮脏 fail-soft /
     首轮干净零重试 / 批量路径脏章回退 / 配额错误不重试）
  C. 停止与全失败成果的可观测性（源码接线锁 + 行为锁）
  D. 配置默认与向后兼容

运行：pytest tests/test_r114_outline_placeholder_closeout_20261020.py -q
"""
from __future__ import annotations

import ast
import copy
import inspect
import json
from pathlib import Path

from app.config import settings
from app.routers import sections
from app.routers import sse_handlers as sh
from app.services import outline_title_fix as otf
from app.services.outline_quality import detect_generic_titles

_BACKEND = Path(__file__).resolve().parents[1]
_SRC_SECTIONS = _BACKEND / "app" / "routers" / "sections.py"
_SRC_UPLOAD = _BACKEND / "app" / "routers" / "upload_outline.py"
_SRC_SSE = _BACKEND / "app" / "routers" / "sse_handlers.py"

#: 用户原话里的三类占位形态（含「（N）」冲突后缀）
PLACEHOLDER_CHILDREN = [
    {"title": "专项6", "description": "占位", "children": []},
    {"title": "总体实施策划（2）", "description": "占位", "children": []},
    {"title": "总体实施策划（3）", "description": "占位", "children": []},
]
CLEAN_CHILDREN = [
    {"title": "灌注桩支护施工", "description": "支护结构实施", "children": []},
    {"title": "钢内支撑安装与拆除", "description": "内支撑体系", "children": []},
]


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 夹具：AI 调用替身
# ---------------------------------------------------------------------------
def _install_ai(monkeypatch, responses):
    """把 ``collect_json_response`` 换成按序返回 responses 的替身。

    responses 元素可以是 dict（作为 AI 的 JSON 对象返回）或 Exception 实例
    （抛出以模拟 provider 失败）。返回 prompts 列表（记录每次实际发出的提示词，
    供「回灌是否生效」断言）。
    """
    prompts: list = []

    async def _fake_collect(messages, validate_fn=None, **kwargs):
        idx = len(prompts)
        prompts.append(messages)
        item = responses[idx] if idx < len(responses) else responses[-1]
        if isinstance(item, Exception):
            raise item
        return item, _dumps(item)

    async def _fake_stats(coro, push_stats, interval: float = 1.0, task_id=None):
        return await coro

    async def _noop_wait_resume(_task_id):
        return None

    monkeypatch.setattr(sh, "collect_json_response", _fake_collect)
    monkeypatch.setattr(sh, "_await_with_stats", _fake_stats)
    monkeypatch.setattr(sh, "wait_resume", _noop_wait_resume)
    monkeypatch.setattr(sh, "is_stopped", lambda _tid: False)
    return prompts


# ===========================================================================
# A. 落库链路上下文（FIX-A）
# ===========================================================================
class TestSavePathContext:
    def test_signature_defaults_are_backward_compatible(self):
        """六个上下文参数必须**可选且默认空串** ⇒ 旧调用点逐字等价。"""
        sig = inspect.signature(otf.autofix_generic_titles_on_save)
        for name in ("construction_scope", "project_brief", "project_facts",
                     "scheme_basis", "standards_text", "requirements"):
            assert name in sig.parameters, f"缺少上下文参数 {name}"
            assert sig.parameters[name].default == "", (
                f"{name} 默认值必须为空串（向后兼容），实际 "
                f"{sig.parameters[name].default!r}")

    async def test_context_is_forwarded_to_title_fix(self, monkeypatch):
        """上下文必须真的透传到 ``_try_outline_title_fix``（否则等于没加）。"""
        captured: dict = {}

        async def _fake_fix(outline, hits, **kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr(otf, "_try_outline_title_fix", _fake_fix)
        monkeypatch.setattr(settings, "outline_autofix_generic_on_save", True,
                            raising=False)
        outline = [{"title": "专项6", "children": []}]
        hits = [{"path": "1", "title": "专项6", "kind": "generic"}]

        await otf.autofix_generic_titles_on_save(
            outline, hits,
            scheme_name="深基坑支护专项施工方案", scheme_type="基坑工程",
            construction_scope="CS-施工内容", project_brief="PB-项目摘要",
            project_facts="PF-事实", scheme_basis="SB-名称解析",
            standards_text="ST-规范", requirements="RQ-编制要求")

        assert captured["scheme_name"] == "深基坑支护专项施工方案"
        assert captured["scheme_type"] == "基坑工程"
        assert captured["construction_scope"] == "CS-施工内容"
        assert captured["project_brief"] == "PB-项目摘要"
        assert captured["project_facts"] == "PF-事实"
        assert captured["scheme_basis"] == "SB-名称解析"
        assert captured["standards_text"] == "ST-规范"
        assert captured["requirements"] == "RQ-编制要求"

    async def test_build_generic_fix_context_assembles_fields(self):
        """上下文装配：scheme/type/requirements/project_brief 必须落地。"""

        class _Cursor:
            def __init__(self, row):
                self._row = row

            async def fetchone(self):
                return self._row

        class _DB:
            async def execute(self, sql, params=()):
                if "FROM schemes" in sql:
                    return _Cursor({
                        "name": "深基坑支护专项施工方案",
                        "type": "基坑工程",
                        "project_id": "p1",
                        "config_json": _dumps({"requirements": "必须包含监测方案"}),
                    })
                if "FROM projects" in sql:
                    return _Cursor({"name": "某某大厦", "engineering_type": "基坑工程"})
                return _Cursor(None)

        ctx = await sections._build_generic_fix_context(_DB(), "sid")
        assert ctx["scheme_name"] == "深基坑支护专项施工方案"
        assert ctx["scheme_type"] == "基坑工程"
        assert ctx["requirements"] == "必须包含监测方案"
        assert ctx["project_brief"] == "工程类型：基坑工程，项目名称：某某大厦"
        # 服务层解析结果只断言「是字符串」（具体拆分由 scheme_scope 自己的护栏锁）
        assert isinstance(ctx["construction_scope"], str)
        assert isinstance(ctx["scheme_basis"], str)

    async def test_build_generic_fix_context_fail_soft(self):
        """DB 读失败 ⇒ 返回空上下文（fail-soft），绝不抛到保存链路。"""

        class _DB:
            async def execute(self, sql, params=()):
                raise RuntimeError("db locked")

        ctx = await sections._build_generic_fix_context(_DB(), "sid")
        assert ctx == {"scheme_name": "", "scheme_type": "", "construction_scope": "",
                       "project_brief": "", "scheme_basis": "", "requirements": ""}

    def test_save_outline_call_passes_context(self):
        """``sections._save_outline_to_db`` 的调用点必须传满上下文（AST 锁）。"""
        tree = ast.parse(_SRC_SECTIONS.read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "autofix_generic_titles_on_save"
        ]
        assert calls, "sections 里必须存在 autofix_generic_titles_on_save 调用"
        required = {"scheme_name", "scheme_type", "construction_scope",
                    "project_brief", "scheme_basis", "requirements"}
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            missing = required - kwargs
            assert not missing, f"save-outline 调用点漏传上下文: {missing}"

    def test_upload_outline_call_passes_context(self):
        """``upload_outline.save_as_outline`` 旧实现连 scheme_name 都没传（AST 锁）。"""
        tree = ast.parse(_SRC_UPLOAD.read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "autofix_generic_titles_on_save"
        ]
        assert calls, "upload_outline 里必须存在 autofix_generic_titles_on_save 调用"
        required = {"scheme_name", "scheme_type", "construction_scope",
                    "project_brief", "scheme_basis", "requirements"}
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            missing = required - kwargs
            assert not missing, f"upload 调用点漏传上下文: {missing}"


# ===========================================================================
# B. 生成期逐章占位闸门（FIX-B）
# ===========================================================================
class TestChildrenGate:
    def _outline(self):
        return [{"title": "基坑支护施工", "children": copy.deepcopy(PLACEHOLDER_CHILDREN)}]

    def test_detects_placeholder_children(self):
        hits = sh._outline_generic_hits(self._outline())
        titles = {h["title"] for h in hits}
        assert "专项6" in titles
        assert "总体实施策划（2）" in titles
        assert "总体实施策划（3）" in titles
        # 干净子目录必须零命中（防误伤）
        assert sh._outline_generic_hits(list(CLEAN_CHILDREN)) == []

    def test_gate_switch_off(self, monkeypatch):
        """``outline_reject_generic_children=False`` ⇒ 逐字回到引入前。"""
        monkeypatch.setattr(settings, "outline_reject_generic_children", False,
                            raising=False)
        assert sh._outline_generic_hits(self._outline()) == []

    def test_master_switch_off(self, monkeypatch):
        """``outline_check_generic_titles=False`` 总闸同样整项失效。"""
        monkeypatch.setattr(settings, "outline_check_generic_titles", False,
                            raising=False)
        assert sh._outline_generic_hits(self._outline()) == []

    async def test_dirty_first_round_triggers_retry_with_feedback(self, monkeypatch):
        """首轮脏 → 必须重试一次，且第二轮提示词里带上一轮的占位标题（回灌）。"""
        prompts = _install_ai(monkeypatch, [
            {"outline": copy.deepcopy(PLACEHOLDER_CHILDREN)},
            {"outline": copy.deepcopy(CLEAN_CHILDREN)},
        ])
        status, children = await sh._fetch_chapter_children(
            0, {"title": "基坑支护施工"},
            sub_prompt=[{"role": "system", "content": "BASE-PROMPT"}],
            task_id="t1", sem=None, validate_fn=sh._sublevel_validate_fn,
            timeout=5, push_stats=None)

        assert status == "ok"
        assert [c["title"] for c in children] == ["灌注桩支护施工", "钢内支撑安装与拆除"]
        assert len(prompts) == 2, "首轮脏必须重试恰好一次"
        feedback = prompts[1][-1]["content"]
        assert "专项6" in feedback and "总体实施策划（2）" in feedback
        assert "BASE-PROMPT" == prompts[1][0]["content"], "原提示词必须原样保留"

    async def test_dirty_both_rounds_fail_soft(self, monkeypatch):
        """两轮都脏 ⇒ fail-soft 返回 ok（下游审核链仍会再修），不得判 failed。"""
        prompts = _install_ai(monkeypatch, [
            {"outline": copy.deepcopy(PLACEHOLDER_CHILDREN)},
            {"outline": copy.deepcopy(PLACEHOLDER_CHILDREN)},
        ])
        status, children = await sh._fetch_chapter_children(
            0, {"title": "基坑支护施工"},
            sub_prompt=[{"role": "system", "content": "BASE-PROMPT"}],
            task_id="t1", sem=None, validate_fn=sh._sublevel_validate_fn,
            timeout=5, push_stats=None)

        assert status == "ok"
        assert len(prompts) == 2
        assert [c["title"] for c in children][0] == "专项6"

    async def test_retry_call_still_carries_scene(self, monkeypatch):
        """行为锁：首试与「占位重试」**两次**实际调用都必须带 scene/json_mode/低温。

        ✅ R114 重锚：旧护栏（test_outline_call_optimization::test_sublevel_sites）
        用「源码里 scene 出现 2 次」锁同一语义；本轮把「失败重试」与「占位重试」
        合并为同一份重试预算（单调用点跑两次），文本计数不再适用 —— 语义改由
        本用例在**行为层**锁死（每次真实调用都打 scene）。
        """
        seen: list = []

        async def _fake_collect(messages, validate_fn=None, **kwargs):
            seen.append(kwargs)
            if len(seen) == 1:
                return {"outline": copy.deepcopy(PLACEHOLDER_CHILDREN)}, "{}"
            return {"outline": copy.deepcopy(CLEAN_CHILDREN)}, "{}"

        async def _fake_stats(coro, push_stats, interval: float = 1.0, task_id=None):
            return await coro

        async def _noop(_tid):
            return None

        monkeypatch.setattr(sh, "collect_json_response", _fake_collect)
        monkeypatch.setattr(sh, "_await_with_stats", _fake_stats)
        monkeypatch.setattr(sh, "wait_resume", _noop)
        monkeypatch.setattr(sh, "is_stopped", lambda _tid: False)

        await sh._fetch_chapter_children(
            0, {"title": "章"},
            sub_prompt=[{"role": "system", "content": "P"}],
            task_id="t1", sem=None, validate_fn=sh._sublevel_validate_fn,
            timeout=5, push_stats=None)

        assert len(seen) == 2
        for kw in seen:
            assert kw.get("scene") == "outline_sublevel"
            assert kw.get("json_mode") is True
            assert kw.get("temperature") == 0.2

    async def test_clean_first_round_no_retry(self, monkeypatch):
        """首轮干净 ⇒ 零额外调用（成本红线）。"""
        prompts = _install_ai(monkeypatch, [
            {"outline": copy.deepcopy(CLEAN_CHILDREN)},
        ])
        status, children = await sh._fetch_chapter_children(
            0, {"title": "基坑支护施工"},
            sub_prompt=[{"role": "system", "content": "BASE-PROMPT"}],
            task_id="t1", sem=None, validate_fn=sh._sublevel_validate_fn,
            timeout=5, push_stats=None)

        assert status == "ok"
        assert len(prompts) == 1
        assert children[0]["title"] == "灌注桩支护施工"

    async def test_quota_error_does_not_retry(self, monkeypatch):
        """配额/认证类错误仍维持「不重试」既有契约（O8/O9 闸门不回归）。"""
        monkeypatch.setattr(sh, "AI_RETRY_ON_QUOTA_ERROR", False)
        monkeypatch.setattr(sh, "_is_quota_error", lambda _e: True)
        prompts = _install_ai(monkeypatch, [RuntimeError("HTTP 402 余额不足")])
        status, children = await sh._fetch_chapter_children(
            0, {"title": "基坑支护施工"},
            sub_prompt=[{"role": "system", "content": "BASE-PROMPT"}],
            task_id="t1", sem=None, validate_fn=sh._sublevel_validate_fn,
            timeout=5, push_stats=None)

        assert status == "failed" and children == []
        assert len(prompts) == 1

    async def test_batch_dirty_chapter_falls_back_to_single(self, monkeypatch):
        """批量响应里某章脏 ⇒ 该章回退单章重写，其它章原样采纳。"""
        monkeypatch.setattr(sh, "is_stopped", lambda _tid: False)
        _install_ai(monkeypatch, [
            {"chapters": [
                {"chapter_id": "1", "outline": copy.deepcopy(PLACEHOLDER_CHILDREN)},
                {"chapter_id": "2", "outline": copy.deepcopy(CLEAN_CHILDREN)},
            ]},
        ])
        called: list = []

        async def _fake_single(index, chapter, **kwargs):
            called.append(index)
            return "ok", copy.deepcopy(CLEAN_CHILDREN)

        monkeypatch.setattr(sh, "_fetch_chapter_children", _fake_single)

        unit_status, per = await sh._fetch_unit_children(
            [(0, {"title": "章一"}), (1, {"title": "章二"})],
            batch_prompt=[{"role": "system", "content": "BATCH"}],
            single_prompts=[[{"role": "system", "content": "S1"}],
                            [{"role": "system", "content": "S2"}]],
            task_id="t1", sem=None, timeout=5, push_stats=None)

        assert unit_status == "ok"
        assert called == [0], "只有脏章回退单章"
        assert [s for s, _ in per] == ["ok", "ok"]
        assert per[0][1][0]["title"] == "灌注桩支护施工"

    async def test_batch_fallback_failure_keeps_batch_result(self, monkeypatch):
        """单章重写失败 ⇒ 沿用批量结果（脏但绝不丢章）。"""
        monkeypatch.setattr(sh, "is_stopped", lambda _tid: False)
        _install_ai(monkeypatch, [
            {"chapters": [
                {"chapter_id": "1", "outline": copy.deepcopy(PLACEHOLDER_CHILDREN)},
                {"chapter_id": "2", "outline": copy.deepcopy(CLEAN_CHILDREN)},
            ]},
        ])

        async def _fake_single(index, chapter, **kwargs):
            return "failed", []

        monkeypatch.setattr(sh, "_fetch_chapter_children", _fake_single)

        unit_status, per = await sh._fetch_unit_children(
            [(0, {"title": "章一"}), (1, {"title": "章二"})],
            batch_prompt=[{"role": "system", "content": "BATCH"}],
            single_prompts=[[{"role": "system", "content": "S1"}],
                            [{"role": "system", "content": "S2"}]],
            task_id="t1", sem=None, timeout=5, push_stats=None)

        assert unit_status == "ok"
        assert [s for s, _ in per] == ["ok", "ok"]
        # 脏章单章重写失败 → 沿用批量结果（绝不丢章；下游审核链再修）
        assert per[0][1][0]["title"] == "专项6"
        assert per[1][1][0]["title"] == "灌注桩支护施工"

    async def test_retry_exception_keeps_first_dirty_result(self, monkeypatch):
        """✅ FIX-B2：首轮脏、重试**抛错** ⇒ 必须保留首轮产出（不判 failed）。

        失败语义对比：旧实现两轮共用一份预算时，第二轮抛错直接 ``failed``，
        编排层把该章计为无子目录 ⇒ 用户看到**整章凭空消失**。首轮虽有占位
        标题，但它是完整可用的二三级结构，保留它（交给下游审核链修）永远
        优于丢章。
        """
        prompts = _install_ai(monkeypatch, [
            {"outline": copy.deepcopy(PLACEHOLDER_CHILDREN)},
            RuntimeError("provider 502"),
        ])

        status, children = await sh._fetch_chapter_children(
            0, {"title": "基坑支护施工"},
            sub_prompt=[{"role": "system", "content": "BASE-PROMPT"}],
            task_id="t1", sem=None, validate_fn=sh._sublevel_validate_fn,
            timeout=5, push_stats=None)

        assert status == "ok", "首轮有产出时重试失败不得判 failed（绝不丢章）"
        assert len(prompts) == 2
        assert [c["title"] for c in children][0] == "专项6"

    async def test_quota_error_after_dirty_keeps_first_result(self, monkeypatch):
        """首轮脏后重试遇配额错误（不重试分支）同样必须保留首轮产出。"""
        monkeypatch.setattr(sh, "AI_RETRY_ON_QUOTA_ERROR", False)
        monkeypatch.setattr(sh, "_is_quota_error", lambda _e: True)
        _install_ai(monkeypatch, [
            {"outline": copy.deepcopy(PLACEHOLDER_CHILDREN)},
            RuntimeError("HTTP 402 余额不足"),
        ])

        status, children = await sh._fetch_chapter_children(
            0, {"title": "基坑支护施工"},
            sub_prompt=[{"role": "system", "content": "BASE-PROMPT"}],
            task_id="t1", sem=None, validate_fn=sh._sublevel_validate_fn,
            timeout=5, push_stats=None)

        assert status == "ok"
        assert [c["title"] for c in children][0] == "专项6"


# ===========================================================================
# C. 停止 / 全失败成果的可观测性（FIX-C）
# ===========================================================================
class TestStoppedPartialObservability:
    def test_stop_exit_wires_generic_probe(self):
        """``_finish_stopped_with_partial`` 必须先过占位检出（源码接线锁）。"""
        tree = ast.parse(_SRC_SSE.read_text(encoding="utf-8"))
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "_finish_stopped_with_partial"),
            None)
        assert fn is not None, "未找到 _finish_stopped_with_partial"
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert "_outline_generic_hits" in names, (
            "停止出口必须调用 _outline_generic_hits 留痕（否则占位残留无法归因到停止链路）")

    async def test_stopped_payload_still_carries_outline(self, monkeypatch):
        """行为锁：检出逻辑不得改变 stopped 载荷契约（outline/partial 仍在）。"""
        saved: dict = {}

        async def _fake_ckpt(task_id, payload):
            saved.update(payload)

        async def _fake_finish(task_id, status, msg=""):
            saved["_status"] = status

        monkeypatch.setattr(sh, "_save_outline_checkpoint", _fake_ckpt)
        monkeypatch.setattr(sh, "finish_task", _fake_finish)

        holder = {"outline": copy.deepcopy(PLACEHOLDER_CHILDREN),
                  "failed_chapters": ["第2章"]}
        line = await sh._finish_stopped_with_partial("t1", holder)
        payload = json.loads(line[len("data: "):].strip())

        assert payload["event"] == "stopped"
        assert payload["partial"] is True
        assert payload["outline"][0]["title"] == "专项6"
        assert payload["failed_count"] == 1
        assert saved.get("_status") == "stopped"
        assert holder["outline"] == [], "成果交出后必须清空 holder（防重复落库）"


# ===========================================================================
# D. 配置默认与判据覆盖
# ===========================================================================
class TestConfigAndDetection:
    def test_new_switch_default_true(self):
        assert getattr(settings, "outline_reject_generic_children", None) is True

    def test_detector_covers_user_reported_shapes(self):
        """判据必须覆盖用户原话的三类形态（专项6 / 总体实施策划（2）（3））。"""
        hits = detect_generic_titles([{"title": t, "children": []}
                                      for t in ("专项6", "总体实施策划（2）",
                                                "总体实施策划（3）")])
        assert len(hits) == 3
        assert all(h["kind"] in ("generic", "conflict_suffix") for h in hits)


# ===========================================================================
# E. 端到端数据链（落库入口 → 上下文装配 → 重写 → sections 表）
# ===========================================================================
class TestSavePathEndToEnd:
    """A 组用 AST 锁证「代码里写了 kwargs」，本组证「数据真的贯通并落库」。"""

    async def test_placeholder_rewrite_lands_in_sections_table(
            self, db_conn, monkeypatch):
        from app.routers import sections as sec

        await db_conn.execute(
            "INSERT INTO projects (id, name, engineering_type) VALUES (?,?,?)",
            ("p1", "某某大厦深基坑工程", "基坑工程"))
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name, type, config_json)"
            " VALUES (?,?,?,?,?)",
            ("s1", "p1", "深基坑支护专项施工方案", "基坑工程",
             _dumps({"requirements": "必须包含监测方案"})))
        await db_conn.commit()

        captured: dict = {}

        async def _fake_fix(outline, hits, **kwargs):
            captured.update(kwargs)
            # 保持结构不变（_tree_shape 守卫），只改标题
            new = copy.deepcopy(outline)
            for node in new:
                if node.get("title") == "专项6":
                    node["title"] = "灌注桩支护施工"
            return new

        monkeypatch.setattr(otf, "_try_outline_title_fix", _fake_fix)
        monkeypatch.setattr(settings, "outline_autofix_generic_on_save", True,
                            raising=False)

        result = await sec._save_outline_to_db(
            db_conn, "s1",
            [{"title": "工程概况", "children": []},
             {"title": "专项6", "children": [
                 {"title": "总体实施策划（2）", "children": []}]}],
            source="e2e-test")

        # ① 上下文真的装配出来了（不是空串）
        assert captured.get("scheme_name") == "深基坑支护专项施工方案"
        assert captured.get("scheme_type") == "基坑工程"
        assert captured.get("requirements") == "必须包含监测方案"
        assert "基坑工程" in (captured.get("project_brief") or "")
        assert isinstance(captured.get("construction_scope"), str)
        assert isinstance(captured.get("scheme_basis"), str)

        # ② 改写结果真的落进了 sections 表
        cur = await db_conn.execute(
            "SELECT title FROM sections WHERE scheme_id=? ORDER BY sort_order",
            ("s1",))
        titles = [r[0] for r in await cur.fetchall()]
        assert "灌注桩支护施工" in titles, titles
        assert "专项6" not in titles, titles

        # ③ 返回契约不变（ok/count/tree 齐备）
        assert result["ok"] is True
        assert result["count"] == len(titles)
        assert isinstance(result["tree"], list)
