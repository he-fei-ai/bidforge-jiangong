# -*- coding: utf-8 -*-
"""R133 · 《标书智能体（2）》目录五机制引入护栏（2026-10-23）

引入文档：《标书智能体（2）——生成标书提纲代码+提示词》《AI 标书提纲生成
工作流程》《目录章节提纲生成工作流程：文档梳理与引入提示词》三份附件。

五机制对照本软件（三级目录 MAX_OUTLINE_DEPTH=3 的实际约束）：
  ① 代码生成框架 + AI 补全 —— outline_framework.build_sublevel_framework
     （等价 generate_one_outline_json_by_level1；数量为本软件硬约束
     2~3 二级 / 1~3 三级的确定性轮转分配，非文档假设的 L2 3~5/L4 4~7）
  ② check_json 结构递归比对 —— outline_framework.check_json + sse_handlers
     _make_framework_validator / _make_framework_batch_validator 闭包
  ③ 最多 3 次定向修复 —— 首试 + MAX_REPAIR_RETRIES=2，严格骨架比对只占
     前 FRAMEWORK_STRICT_ROUNDS 次，第 3 次 fail-soft 降级放行（绝不丢章）
  ④ other_outline 防重复 —— 既有机制（_build_unit_prompts 恒传），本文件
     只锁「并发窗口共享 prior 快照」不回潮
  ⑤ 并发生成 —— 既有 _window × _merge_k 编排，gather 保序归位

分组：
  A. 骨架构建（约束/确定性/rng 随机/结构同构）
  B. check_json 递归比对（路径报错/多余键放行/数字互换/bool 严格）
  C. fill_framework_structure 结构兜底（fail-soft、不增造节点）
  D. 校验闭包（严格轮次有界 + 第 3 次降级 + 形状校验恒优先）
  E. 分步链路接线（_fetch_unit_children 吃 frameworks、fill 收口）
  F. 提示词侧（独占行可选区块、契约登记、开关默认）
  G. R133 BUG 修复：str 形态 sub_prompt 的占位回灌不再字符化

运行：pytest tests/test_outline_framework_r133_20261023.py -q
"""
from __future__ import annotations

import copy
import json
import random

from app.config import settings
from app.routers import sse_handlers as sh
from app.services import outline_framework as of
from app.services.ai.prompts._registry import (
    _is_optional_block_var,
    check_prompt_variables,
    get_default_prompt,
    render_prompt,
)

CLEAN = [
    {"title": "灌注桩支护施工", "description": "支护桩成孔与浇筑", "children": []},
    {"title": "钢内支撑安装与拆除", "description": "支撑体系施工", "children": []},
]


# ===========================================================================
# A. 骨架构建（机制①）
# ===========================================================================
class TestBuildFramework:
    def test_structure_and_count_constraints(self):
        """2~3 二级 / 1~3 三级硬约束 + id 格式 + title/description 留空。"""
        for idx in range(1, 8):
            fw = of.build_sublevel_framework(idx, f"第{idx}章")
            assert set(fw) == {"outline"}
            ol = fw["outline"]
            assert of.L2_MIN <= len(ol) <= of.L2_MAX, f"第{idx}章二级数越界"
            for j, l2 in enumerate(ol, start=1):
                assert set(l2) == {"id", "title", "description", "children"}
                assert l2["id"] == f"{idx}.{j}"
                assert l2["title"] == "" and l2["description"] == ""
                kids = l2["children"]
                assert of.L3_MIN <= len(kids) <= of.L3_MAX, f"第{idx}.{j}三级数越界"
                for k, l3 in enumerate(kids, start=1):
                    assert l3["id"] == f"{idx}.{j}.{k}"
                    assert l3["title"] == "" and l3["children"] == []

    def test_deterministic_rotation_adjacent_chapters_differ(self):
        """rng=None：按章序轮转分配 —— 相邻章数量有差异、同章可复现。"""
        n1 = len(of.build_sublevel_framework(1)["outline"])
        n2 = len(of.build_sublevel_framework(2)["outline"])
        assert n1 != n2, "相邻章二级数量必须不同（对应参考实现随机分布初衷）"
        assert of.build_sublevel_framework(1) == of.build_sublevel_framework(1), \
            "同章多次构建必须一致（可测的确定性）"

    def test_rng_restores_random_distribution(self):
        """传 rng 恢复参考实现的随机语义（nodes_distribution 等价物）。"""
        rng = random.Random(42)
        for _ in range(20):
            fw = of.build_sublevel_framework(1, rng=rng)
            assert of.L2_MIN <= len(fw["outline"]) <= of.L2_MAX
            for l2 in fw["outline"]:
                assert of.L3_MIN <= len(l2["children"]) <= of.L3_MAX

    def test_shape_is_same_as_sublevel_response(self):
        """骨架与子目录 AI 响应同构（{"outline": [...]}），供 check_json 比对。"""
        fw = of.build_sublevel_framework(3, "第三章")
        # 同构：把 title/description 填上即为合法子目录响应形状
        for l2 in fw["outline"]:
            l2["title"] = "填好的二级"
            l2["description"] = "填好的说明"
            for l3 in l2["children"]:
                l3["title"] = "填好的三级"
        ok, err = of.check_json(fw, of.build_sublevel_framework(3, "第三章"))
        assert ok, err


# ===========================================================================
# B. check_json（机制②）
# ===========================================================================
class TestCheckJson:
    def test_identical_passes(self):
        fw = of.build_sublevel_framework(1, "第一章")
        ok, err = of.check_json(json.dumps(fw, ensure_ascii=False), fw)
        assert ok and err == ""

    def test_missing_key_reports_path(self):
        fw = of.build_sublevel_framework(1, "第一章")
        bad = copy.deepcopy(fw)
        del bad["outline"][0]["children"]      # 缺必需键
        ok, err = of.check_json(bad, fw)
        assert not ok and "children" in err and "outline[0]" in err, err

    def test_type_mismatch_reports_path(self):
        fw = of.build_sublevel_framework(1, "第一章")
        bad = copy.deepcopy(fw)
        bad["outline"][0]["children"] = "不是列表"
        ok, err = of.check_json(bad, fw)
        assert not ok and "类型不匹配" in err and "outline[0].children" in err, err

    def test_extra_keys_allowed(self):
        """AI 携带 id 等附加字段不得判违规（多余键放行）。"""
        fw = of.build_sublevel_framework(1, "第一章")
        tgt = copy.deepcopy(fw)
        tgt["outline"][0]["extra_field"] = "ok"
        tgt["outline"][0]["title"] = "有内容"
        ok, err = of.check_json(tgt, fw)
        assert ok, err

    def test_numbers_interchangeable_but_bool_is_not(self):
        tpl = {"n": 1, "flag": True}
        assert of.check_json({"n": 1.5, "flag": True}, tpl)[0] is True
        # bool 不吃「数字互换」豁免：True 与 1 类型判定必须走严格分支
        assert of.check_json({"n": 1, "flag": 1}, tpl)[0] is False

    def test_list_rules(self):
        tpl = {"items": [{"a": 1}]}
        assert of.check_json({"items": []}, tpl)[0] is False, "模板非空目标空必须报错"
        assert of.check_json({"items": [{"a": 2}, {"a": 3}]}, tpl)[0] is True, \
            "列表不比长度，用首元素模板校验每项"
        assert of.check_json({"items": [1]}, tpl)[0] is False
        # 模板空列表 → 任意列表放行
        assert of.check_json({"items": [1, 2]}, {"items": []})[0] is True

    def test_garbage_input_never_raises(self):
        ok, err = of.check_json("not json", {"a": 1})
        assert not ok and err
        ok, err = of.check_json(None, {"a": 1})
        assert not ok and err


# ===========================================================================
# C. fill_framework_structure（机制①最终兜底）
# ===========================================================================
class TestFillFramework:
    def test_enforces_three_keys_without_fabricating_nodes(self):
        """结构以骨架为准（三键齐）、数量以 AI 为准（不增造占位节点）。"""
        fw = of.build_sublevel_framework(1, "第一章")
        ai = [{"title": "1.1 灌注桩施工", "description": "成孔浇筑"}]  # 缺 children、只 1 个
        out = of.fill_framework_structure(fw, ai)
        assert len(out) == 1, "AI 少给的节点不得凭空补齐（防占位标题）"
        assert set(out[0]) == {"title", "description", "children"}
        assert out[0]["children"] == []
        assert out[0]["title"] == "1.1 灌注桩施工", "标题内容原样保留"

    def test_extra_ai_nodes_kept_not_dropped(self):
        fw = of.build_sublevel_framework(1, "第一章")
        ai = copy.deepcopy(CLEAN) + [copy.deepcopy(CLEAN[0])]
        out = of.fill_framework_structure(fw, ai)
        assert len(out) == len(ai), "AI 多给的节点不得被丢弃（丢内容=丢章）"

    def test_fail_soft_on_garbage(self):
        """任何异常/非法输入都不得抛出（fail-soft 红线）。"""
        assert of.fill_framework_structure(None, "x") == []
        assert of.fill_framework_structure({"outline": "bad"}, None) == []
        # framework 非法但 ai 合法：仍按空骨架规整出三键结构
        out = of.fill_framework_structure({"outline": 123}, copy.deepcopy(CLEAN))
        assert all(set(c) == {"title", "description", "children"} for c in out)


# ===========================================================================
# D. 校验闭包（机制③：有界严格 + 第 3 次降级）
# ===========================================================================
class TestValidators:
    def _fw(self):
        return of.build_sublevel_framework(1, "第一章")

    def _bad_obj(self):
        return {"outline": [{"title": "缺 children 的节点", "description": "d"}]}

    def test_strict_rounds_then_graceful_release(self):
        v = sh._make_framework_validator(lambda o: [], self._fw())
        # 前 FRAMEWORK_STRICT_ROUNDS 次：结构比对不一致 → 带路径 issues
        for _ in range(sh.FRAMEWORK_STRICT_ROUNDS):
            issues = v(self._bad_obj())
            assert issues and "骨架不一致" in issues[0], issues
        # 第 3 次起：fail-soft 降级放行（绝不因骨架比对不过而炸整章）
        assert v(self._bad_obj()) == []
        assert v(self._bad_obj()) == [], "降级状态必须保持（不反复严格）"

    def test_shape_issues_always_win_over_release(self):
        """降级轮里形状校验仍生效（放行≠无底线）。"""
        v = sh._make_framework_validator(lambda o: ["形状坏了"], self._fw())
        assert v(self._bad_obj()) == ["形状坏了"]
        assert v(self._bad_obj()) == ["形状坏了"], "形状问题不享受降级放行"

    def test_matching_output_always_passes(self):
        fw = self._fw()
        good = copy.deepcopy(fw)
        for l2 in good["outline"]:
            l2["title"] = "二级标题"
            for l3 in l2["children"]:
                l3["title"] = "三级标题"
        v = sh._make_framework_validator(lambda o: [], fw)
        for _ in range(4):
            assert v(good) == []

    def test_no_framework_is_plain_shape_check(self):
        """framework=None（开关关闭/旧调用点）⇒ 逐字退回旧口径。"""
        v = sh._make_framework_validator(lambda o: ["x"], None)
        assert v({"outline": []}) == ["x"]

    def test_batch_validator_aligns_by_chapter_id(self):
        fws = {"1": self._fw()}
        v = sh._make_framework_batch_validator(lambda o: [], fws)
        obj = {"chapters": [
            {"chapter_id": "1", "outline": [{"title": "缺 children"}]},
            {"chapter_id": "99", "outline": []},   # 无骨架章：只做形状校验
        ]}
        issues = v(obj)
        assert issues and "chapter 1" in issues[0], issues
        # 降级放行同样有界
        for _ in range(sh.FRAMEWORK_STRICT_ROUNDS - 1):
            v(obj)
        assert v(obj) == []

    def test_fill_or_keep_identity_without_framework(self):
        kids = copy.deepcopy(CLEAN)
        assert sh._framework_fill_or_keep(None, kids) is kids
        out = sh._framework_fill_or_keep(self._fw(), kids)
        assert all(set(c) == {"title", "description", "children"} for c in out)


# ===========================================================================
# E. 分步链路接线（frameworks 真的流经 _fetch_unit_children）
# ===========================================================================
def _install_ai(monkeypatch, responses):
    """同 test_r114 夹具口径：按序返回响应 + 记录每次 validate_fn 的 issues。"""
    prompts: list = []
    seen_issues: list = []

    async def _fake_collect(messages, validate_fn=None, **kwargs):
        idx = len(prompts)
        prompts.append(messages)
        item = responses[idx] if idx < len(responses) else responses[-1]
        if isinstance(item, Exception):
            raise item
        if validate_fn:
            seen_issues.append(validate_fn(item))
        return item, json.dumps(item, ensure_ascii=False)

    async def _fake_stats(coro, push_stats, interval: float = 1.0, task_id=None):
        return await coro

    monkeypatch.setattr(sh, "collect_json_response", _fake_collect)
    monkeypatch.setattr(sh, "_await_with_stats", _fake_stats)
    monkeypatch.setattr(sh, "wait_resume", lambda _tid: _noop())
    monkeypatch.setattr(sh, "is_stopped", lambda _tid: False)
    return prompts, seen_issues


async def _noop():
    return None


class TestUnitChildrenWiring:
    async def test_single_chapter_strict_then_fill(self, monkeypatch):
        """单章分支：frameworks→严格校验闭包 + fill 收口（缺 children 被补键）。"""
        fw = of.build_sublevel_framework(1, "第一章")
        resp = {"outline": [{"title": "灌注桩支护施工", "description": "d"}]}
        _prompts, issues = _install_ai(monkeypatch, [resp])
        st, per = await sh._fetch_unit_children(
            [(0, {"title": "第一章"})],
            batch_prompt=None, single_prompts=[["role", "content"]],
            task_id="t1", sem=None, timeout=5, push_stats=None,
            frameworks={"1": fw})
        assert st == "ok"
        children = per[0][1]
        assert all(set(c) == {"title", "description", "children"} for c in children), \
            "fill 规整必须生效（AI 漏 children 键被补上）"
        assert issues and issues[0], "首调必须触发骨架严格比对并报出 issues"

    async def test_batch_chapter_alignment_and_fill(self, monkeypatch):
        """批量分支：按 chapter_id 对位比对 + 归位后逐章 fill。"""
        fws = {"1": of.build_sublevel_framework(1, "第一章"),
               "2": of.build_sublevel_framework(2, "第二章")}
        resp = {"chapters": [
            {"chapter_id": "1",
             "outline": [{"title": "灌注桩支护施工", "description": "d"}]},
            {"chapter_id": "2",
             "outline": [{"title": "土方开挖施工", "description": "d"}]},
        ]}
        _prompts, issues = _install_ai(monkeypatch, [resp])
        st, per = await sh._fetch_unit_children(
            [(0, {"title": "第一章"}), (1, {"title": "第二章"})],
            batch_prompt=["role", "content"], single_prompts=[[], []],
            task_id="t1", sem=None, timeout=5, push_stats=None,
            frameworks=fws)
        assert st == "ok" and len(per) == 2
        assert issues and issues[0], "批量首调必须触发逐章骨架比对"
        for _st, kids in per:
            assert kids and all(set(c) == {"title", "description", "children"}
                                for c in kids)

    async def test_no_frameworks_is_byte_identical_path(self, monkeypatch):
        """不传 frameworks（旧调用点/开关关）⇒ 校验用旧函数、无 fill 包装。"""
        resp = {"outline": copy.deepcopy(CLEAN)}
        _prompts, issues = _install_ai(monkeypatch, [resp])
        st, per = await sh._fetch_unit_children(
            [(0, {"title": "第一章"})],
            batch_prompt=None, single_prompts=[["role", "content"]],
            task_id="t1", sem=None, timeout=5, push_stats=None)
        assert st == "ok"
        assert per[0][1] == CLEAN
        # 旧口径：形状校验被 fake 记录一次，但不是骨架比对闭包（issues 只来自 base）
        assert issues == [[]]


class TestSourceWiring:
    """源码级接线锁：防止后续重构把 frameworks 传递链拆断。"""

    def _src(self) -> str:
        from pathlib import Path

        import app as app_pkg
        return (Path(app_pkg.__file__).parent / "routers"
                / "sse_handlers.py").read_text(encoding="utf-8")

    def test_fetch_unit_children_accepts_frameworks(self):
        src = self._src()
        assert "frameworks: dict | None = None" in src
        assert "frameworks=_w_fws" in src, "编排层必须把 frameworks 传进单元取回"
        assert "return single_prompts, batch_prompt, frameworks" in src, \
            "_build_unit_prompts 必须返回 3 元组"

    def test_all_success_returns_go_through_fill(self):
        """_fetch_chapter_children 的成功返回必须全部经 _ok（fill 收口）。"""
        src = self._src()
        assert src.count("return _ok(") >= 4, \
            "成功返回点（首试/重试后仍脏/配额兜底/重试失败兜底）须全部走 _ok"
        assert 'return "ok", children' not in src
        assert 'return "ok", _fallback' not in src


# ===========================================================================
# F. 提示词侧（独占行可选区块 + 契约 + 开关）
# ===========================================================================
class TestPromptSide:
    _REQUIRED = {
        "outline_sublevel_system": dict(
            chapter_id="1", chapter_title="第一章", chapter_desc="说明",
            scheme_name="X工程", scheme_type="基坑支护",
            construction_scope="scope", standards_text="GB50000",
            project_brief="brief", other_outline="其他章",
            prior_chapters="无", requirements="无", project_facts="facts"),
        "outline_sublevel_batch_system": dict(
            chapter_count=2, chapters_text="- chapter_id=1",
            scheme_name="X工程", scheme_type="基坑支护",
            construction_scope="scope", standards_text="GB50000",
            project_brief="brief", other_outline="其他章",
            prior_chapters="无", requirements="无", project_facts="facts"),
    }

    def test_block_placeholder_is_exclusive_line_in_both_templates(self):
        for key in self._REQUIRED:
            tpl = get_default_prompt(key)
            assert _is_optional_block_var("outline_framework_block", tpl), \
                f"{key} 的 {{outline_framework_block}} 必须独占一整行"

    def test_render_without_block_leaves_no_residue(self):
        for key, kw in self._REQUIRED.items():
            out = render_prompt(get_default_prompt(key), **kw)
            assert "{outline_framework_block}" not in out
            assert "【结构框架" not in out

    def test_render_with_block_injects_framework(self):
        fw = of.build_sublevel_framework(1, "第一章")
        block = of.render_framework_block(fw, "第一章", "说明")
        out = render_prompt(get_default_prompt("outline_sublevel_system"),
                            **self._REQUIRED["outline_sublevel_system"],
                            outline_framework_block=block)
        assert "【结构框架" in out
        assert "禁止修改 JSON 结构" in out
        assert '"outline"' in out

    def test_contract_declared_and_clean(self):
        assert check_prompt_variables() == [], "契约漂移护栏必须干净"

    def test_switch_default_on_and_backoff(self):
        assert settings.outline_framework_fill is True
        assert hasattr(settings, "outline_framework_fill")

    def test_framework_block_rules(self):
        fw = of.build_sublevel_framework(2, "第二章", "土方")
        block = of.render_framework_block(fw, "第二章", "土方")
        for rule in ("禁止修改 JSON 结构", "禁止修改一级标题",
                     "直接返回补全后的完整 JSON"):
            assert rule in block
        assert '"2.1"' in block, "骨架 id 必须出现在注入块中"


# ===========================================================================
# G. R133 BUG：str 形态 sub_prompt 的占位回灌不再逐字符化
# ===========================================================================
class TestStrFeedbackFix:
    async def test_feedback_wraps_str_prompt_into_single_system(self, monkeypatch):
        """真实链路 sub_prompt 是裸 str：回灌轮必须包成 1 条 system 消息。

        旧实现 list(str) 把提示词拆成逐字符列表 → provider 侧 normalize 成
        字符轰炸，R114 FIX-B「带拒收原因重试」在真实链路静默失效。
        """
        placeholder = {"outline": [
            {"title": "专项6", "description": "占位", "children": []}]}
        clean = {"outline": copy.deepcopy(CLEAN)}
        prompts, _issues = _install_ai(monkeypatch, [placeholder, clean])
        base = "BASE-PROMPT-原始系统提示词"
        st, children = await sh._fetch_chapter_children(
            0, {"title": "第一章"},
            sub_prompt=base, task_id="t1", sem=None,
            validate_fn=sh._sublevel_validate_fn, timeout=5, push_stats=None)
        assert st == "ok"
        assert prompts[0] == base, "首试必须原样发出裸 str 提示词"
        assert isinstance(prompts[1], list) and len(prompts[1]) == 2, \
            "回灌轮必须是 [system(原提示词), user(拒收原因)] 两条消息"
        assert prompts[1][0] == {"role": "system", "content": base}, \
            "原提示词必须整段保留为单条 system（不得逐字符拆分）"
        assert "专项6" in prompts[1][-1]["content"], "回灌必须携带占位标题"

    async def test_list_prompt_path_unchanged(self, monkeypatch):
        """list 形态（测试桩口径）走旧写法，不被 str 修复误伤。"""
        placeholder = {"outline": [
            {"title": "专项6", "description": "占位", "children": []}]}
        clean = {"outline": copy.deepcopy(CLEAN)}
        prompts, _ = _install_ai(monkeypatch, [placeholder, clean])
        base = [{"role": "system", "content": "BASE-PROMPT"}]
        st, _children = await sh._fetch_chapter_children(
            0, {"title": "第一章"},
            sub_prompt=base, task_id="t1", sem=None,
            validate_fn=sh._sublevel_validate_fn, timeout=5, push_stats=None)
        assert st == "ok"
        assert prompts[1][0] == {"role": "system", "content": "BASE-PROMPT"}
        assert "专项6" in prompts[1][-1]["content"]
