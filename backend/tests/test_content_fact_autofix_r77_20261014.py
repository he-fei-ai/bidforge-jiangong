# -*- coding: utf-8 -*-
"""R77（2026-10-14）· 正文生成后「事实冲突自动定点修复」单测。

引入背景（对齐参考软件 OpenBidKit 易标 contentGenerationTask.cjs 的
「生成后审计 + old_text/new_text 唯一命中替换」机制）：
  * standard_report 每章检出的 error 级事实冲突（正文数值/型号与本章事实
    矛盾）引入前只写报告、不进修复链路；
  * 本模块（services/content_fact_autofix）把冲突接进「AI 定点编辑 +
    唯一命中才替换 + 失败回灌重试（上限 2 次）」闭环。

覆盖组：
  A. select_fact_conflicts 纯函数值域（只取 error 级事实冲突三类）；
  B. 与 standard_report 的**真实集成**（产出的 issue type 能被选中）；
  C. autofix_section_fact_conflicts 行为（唯一命中替换 / 多处命中拒绝 +
     失败回灌重试 / 空 edits 安全阀 / 异常 fail-soft / 无冲突零调用）；
  D. 静态接线与配置面（sse_handlers 接线 / 提示词注册 / 场景登记 /
     配置默认值）。
"""
import inspect
import json
import os
from pathlib import Path

import pytest

from app.config import settings
from app.services.ai.provider_factory import KNOWN_SCENES
from app.services.content_fact_autofix import (
    FACT_AUTOFIX_MAX_ATTEMPTS,
    FACT_CONFLICT_ISSUE_TYPES,
    autofix_section_fact_conflicts,
    select_fact_conflicts,
)
from app.services.content_standard import standard_report

#: 仓库根（backend/tests/ → 上两级）
_REPO = Path(__file__).resolve().parents[2]


# ---------- 工具 ----------

class _FakeChat:
    """记录每次调用的 messages，按队列返回预置响应（或抛错）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list] = []

    async def __call__(self, messages, **kwargs):
        self.calls.append(list(messages))
        item = self._responses.pop(0) if self._responses else '{"edits": []}'
        if isinstance(item, Exception):
            raise item
        return item


def _conflict_issue(issue_type="value_mismatch", severity="error"):
    return {
        "type": issue_type,
        "severity": severity,
        "message": "「开挖深度」数值与全局事实矛盾：事实 8.6m，正文 9.2m",
        "excerpt": "…基坑开挖深度为 9.2m…",
    }


def _fact_rows():
    # (group_title, title, content, confidence, chapter) —— 5 元组行契约
    return [("基坑支护", "开挖深度", "基坑开挖深度 8.6m，采用灌注桩支护", 1.0, "technique")]


# ---------- A. select_fact_conflicts ----------

def test_select_extracts_three_fact_conflict_types():
    """三类 error 级事实冲突必须全部选中（与 standard_report 值域对齐）。"""
    report = {"issues": [
        _conflict_issue("value_mismatch"),
        _conflict_issue("value_conflict"),
        _conflict_issue("model_conflict"),
    ]}
    out = select_fact_conflicts(report)
    assert [i["type"] for i in out] == [
        "value_mismatch", "value_conflict", "model_conflict"]


def test_select_ignores_non_conflict_types():
    """warning 级缺项（fact_value_missing）与非事实类 error 一律不选。

    依据易标审计纪律「正文没有涉及某条事实时，不要报告缺失」——缺项≠冲突，
    报缺失会诱导 AI 补写编造值。
    """
    report = {"issues": [
        _conflict_issue("fact_value_missing", severity="warning"),
        _conflict_issue("placeholder_mark"),          # error 但非事实冲突
        _conflict_issue("fabricated_date"),           # error 但非事实冲突
        _conflict_issue("fuzzy_expression", severity="warning"),
        {"type": "value_mismatch", "severity": "warning"},  # 类型对但级别不对
    ]}
    assert select_fact_conflicts(report) == []


def test_select_tolerates_bad_shapes():
    """坏输入（None / 非 dict / issues 非列表 / 元素非 dict）一律返回空表。"""
    assert select_fact_conflicts(None) == []
    assert select_fact_conflicts({}) == []
    assert select_fact_conflicts({"issues": "x"}) == []
    assert select_fact_conflicts({"issues": ["x", None]}) == []


def test_conflict_types_tuple_is_frozen_contract():
    """FACT_CONFLICT_ISSUE_TYPES 是与 standard_report 的契约，禁止漂移。"""
    assert FACT_CONFLICT_ISSUE_TYPES == (
        "value_mismatch", "value_conflict", "model_conflict")


# ---------- B. 与 standard_report 的真实集成 ----------

def test_standard_report_conflict_is_selectable():
    """端到端：真实正文冲突 → standard_report → select 能选中。"""
    rows = _fact_rows()
    content = "本工程基坑开挖深度为 9.2m，采用灌注桩支护，详见计算书。"
    report = standard_report(content, "precise", rows,
                             section_id="s1", section_title="基坑开挖")
    conflicts = select_fact_conflicts(report)
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "value_mismatch"
    assert conflicts[0]["severity"] == "error"


def test_clean_section_yields_no_conflicts():
    """正文与事实一致时 select 返回空（无冲突 → 调用方零 AI 成本）。"""
    rows = _fact_rows()
    content = "本工程基坑开挖深度为 8.6m，采用灌注桩支护，详见计算书。"
    report = standard_report(content, "precise", rows,
                             section_id="s1", section_title="基坑开挖")
    assert select_fact_conflicts(report) == []


# ---------- C. autofix_section_fact_conflicts ----------

@pytest.mark.asyncio
async def test_no_conflicts_means_no_ai_call():
    """无冲突 → attempted=False，一次 AI 调用都不发（成本中性）。"""
    chat = _FakeChat([])
    body, res = await autofix_section_fact_conflicts(
        content="正文", standard="precise", fact_rows=_fact_rows(),
        conflicts=[], section_id="s1", chat_fn=chat)
    assert body == "正文"
    assert res["attempted"] is False
    assert res["applied"] == 0
    assert chat.calls == []


@pytest.mark.asyncio
async def test_no_fact_text_means_no_ai_call():
    """事实行为空（渲染不出事实文本）→ 双保险，不发起调用。"""
    chat = _FakeChat([])
    body, res = await autofix_section_fact_conflicts(
        content="正文", standard="precise", fact_rows=[],
        conflicts=[_conflict_issue()], section_id="s1", chat_fn=chat)
    assert body == "正文"
    assert res["attempted"] is False
    assert chat.calls == []


@pytest.mark.asyncio
async def test_unique_hit_replaces_conflicting_value():
    """old_text 唯一命中 → 精确替换，只动冲突处。"""
    content = "本工程基坑开挖深度为 9.2m，采用灌注桩支护。"
    chat = _FakeChat([json.dumps({"edits": [{
        "old_text": "基坑开挖深度为 9.2m",
        "new_text": "基坑开挖深度为 8.6m",
        "reason": "违反事实「开挖深度 8.6m」",
    }]}, ensure_ascii=False)])
    body, res = await autofix_section_fact_conflicts(
        content=content, standard="precise", fact_rows=_fact_rows(),
        conflicts=[_conflict_issue()], section_id="s1",
        section_title="基坑开挖", chat_fn=chat)
    assert body == "本工程基坑开挖深度为 8.6m，采用灌注桩支护。"
    assert res["attempted"] is True
    assert res["applied"] == 1
    assert res["rejected"] == []
    assert res["attempts"] == 1
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_old_text_rejected_then_retried_with_feedback():
    """多处命中一律拒绝 + 失败原因回灌重试（对齐易标普通修复）。

    第一轮 old_text 在正文出现两次 → ambiguous 拒绝；第二轮 AI 带更长上下文
    重新定位 → 唯一命中成功。回灌消息必须包含「上次修复应用失败原因」。
    """
    content = "工期按 120 天组织。赶工阶段工期按 120 天考核，不得延误。"
    first = json.dumps({"edits": [{
        "old_text": "工期按 120 天",
        "new_text": "工期按 90 天",
        "reason": "违反事实「总工期 90 天」",
    }]}, ensure_ascii=False)
    second = json.dumps({"edits": [{
        "old_text": "赶工阶段工期按 120 天考核",
        "new_text": "赶工阶段工期按 90 天考核",
        "reason": "违反事实「总工期 90 天」",
    }]}, ensure_ascii=False)
    chat = _FakeChat([first, second])
    rows = [("工期安排", "项目总工期", "项目总工期 90 天", 1.0, "plan")]
    body, res = await autofix_section_fact_conflicts(
        content=content, standard="precise", fact_rows=rows,
        conflicts=[_conflict_issue()], section_id="s1",
        section_title="施工进度计划", chat_fn=chat)
    assert len(chat.calls) == 2
    # 第二轮必须带上第一轮的失败原因（定向修复，不是重新问）
    feedback = "\n".join(
        str(m.get("content") or "") for m in chat.calls[1]
        if m.get("role") == "user")
    assert "上次修复应用失败原因" in feedback
    assert "ambiguous" in feedback
    assert body == "工期按 120 天组织。赶工阶段工期按 90 天考核，不得延误。"
    assert res["applied"] == 1
    assert res["attempts"] == 2


@pytest.mark.asyncio
async def test_empty_edits_is_safety_valve():
    """AI 判定并非真实矛盾 → 返回空 edits，正文一个字节都不改。"""
    content = "地面采用 18mm 多层板，墙面采用 12mm 竹胶板。"
    chat = _FakeChat(['{"edits": []}'])
    rows = [("模板工程", "模板选材", "模板采用 18mm 多层板", 1.0, "technique")]
    body, res = await autofix_section_fact_conflicts(
        content=content, standard="precise", fact_rows=rows,
        conflicts=[_conflict_issue()], section_id="s1", chat_fn=chat)
    assert body == content
    assert res["attempted"] is True
    assert res["applied"] == 0
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_chat_failure_is_fail_soft():
    """AI 调用持续异常 → fail-soft：正文原样、attempts 打满、不抛。"""
    content = "本工程基坑开挖深度为 9.2m。"
    chat = _FakeChat([RuntimeError("boom"), RuntimeError("boom2")])
    body, res = await autofix_section_fact_conflicts(
        content=content, standard="precise", fact_rows=_fact_rows(),
        conflicts=[_conflict_issue()], section_id="s1", chat_fn=chat)
    assert body == content
    assert res["attempted"] is True
    assert res["applied"] == 0
    assert res["attempts"] == FACT_AUTOFIX_MAX_ATTEMPTS
    assert len(chat.calls) == FACT_AUTOFIX_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_partial_apply_then_giveup_keeps_applied():
    """第一轮部分成功（一条命中一条被拒）、第二轮 AI 放弃 → 保留已应用结果。

    被拒条目的失败原因必须回灌第二轮（定向修复语义）；最终正文只含
    成功替换，不因第二轮放弃而倒退。
    """
    content = "总工期 120 天。质保期 5 年。"
    first = json.dumps({"edits": [
        {"old_text": "总工期 120 天", "new_text": "总工期 90 天",
         "reason": "违反事实「总工期 90 天」"},
        {"old_text": "不存在的原文片段xyz", "new_text": "质保期 3 年",
         "reason": "违反事实「质保期 3 年」"},
    ]}, ensure_ascii=False)
    chat = _FakeChat([first, '{"edits": []}'])
    rows = [("工期安排", "项目总工期", "项目总工期 90 天", 1.0, "plan")]
    body, res = await autofix_section_fact_conflicts(
        content=content, standard="precise", fact_rows=rows,
        conflicts=[_conflict_issue(), _conflict_issue()],
        section_id="s1", chat_fn=chat)
    assert len(chat.calls) == 2
    feedback = "\n".join(
        str(m.get("content") or "") for m in chat.calls[1]
        if m.get("role") == "user")
    assert "上次修复应用失败原因" in feedback
    assert "not_found" in feedback
    assert body == "总工期 90 天。质保期 5 年。"
    assert res["applied"] == 1
    assert res["attempts"] == 2
    assert res["rejected"] and res["rejected"][0]["reason"] == "not_found"


@pytest.mark.asyncio
async def test_malformed_json_then_repaired_by_feedback_round():
    """首轮返回非 JSON → 不放弃，第二轮带反馈重试（有界）。"""
    content = "总工期 120 天。"
    chat = _FakeChat([
        "模型输出了一段解释而不是 JSON",
        json.dumps({"edits": [{
            "old_text": "总工期 120 天", "new_text": "总工期 90 天",
            "reason": "违反事实"}]}, ensure_ascii=False),
    ])
    rows = [("工期安排", "项目总工期", "项目总工期 90 天", 1.0, "plan")]
    body, res = await autofix_section_fact_conflicts(
        content=content, standard="precise", fact_rows=rows,
        conflicts=[_conflict_issue()], section_id="s1", chat_fn=chat)
    assert body == "总工期 90 天。"
    assert res["applied"] == 1


# ---------- D. 静态接线与配置面 ----------

def _src(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_prompts_registered_and_renderable():
    """两组提示词已注册且全部变量可渲染（无残留占位符）。"""
    from app.services.ai.prompts._registry import _ALL_PROMPTS, render
    for key in ("content_fact_autofix_system", "content_fact_autofix_user"):
        assert key in _ALL_PROMPTS, f"提示词未注册: {key}"
    sys_text = render("content_fact_autofix_system")
    assert "old_text" in sys_text and "唯一" in sys_text
    user_text = render(
        "content_fact_autofix_user",
        global_facts="事实", section_id="s1", section_title="标题",
        generation_standard="precise", conflicts="[]")
    assert "{global_facts}" not in user_text
    assert "{conflicts}" not in user_text


def test_scene_registered_in_known_scenes():
    """scene 必须登记进 KNOWN_SCENES（双向漂移护栏 TestKnownScenesDrift）。"""
    assert "content_fact_autofix" in KNOWN_SCENES


def test_scene_used_by_autofix_module():
    """登记不能是死条目：模块必须真实使用该 scene。"""
    src = _src("app/services/content_fact_autofix.py")
    assert 'scene="content_fact_autofix"' in src


def test_sse_handlers_wiring_present():
    """sse_handlers 必须接线：选冲突 + 调修复 + 门控开关三要素齐全。"""
    src = _src("app/routers/sse_handlers.py")
    assert "select_fact_conflicts(report)" in src
    assert "autofix_section_fact_conflicts(" in src
    assert 'getattr(settings, "content_fact_autofix_enabled"' in src
    assert 'report["fact_autofix"]' in src


def test_config_default_enabled():
    """配置默认 True（仅在检出冲突的章节触发，无冲突章节零成本）。"""
    assert settings.content_fact_autofix_enabled is True


def test_outline_wiring_present():
    """目录子目录按章相关性注入的接线三要素（开关 + 加载 + 渲染函数）。

    ⚠️ 断言用「配置键名」而非完整 getattr 串：接线处 getattr 参数跨行书写
    （flake8 行宽），整串断言会假阴性（R75b「假护栏」同族教训）。
    """
    src = _src("app/routers/sse_handlers.py")
    assert "outline_sublevel_facts_relevant" in src
    assert "render_outline_chapter_facts(" in src
    assert "_sublevel_facts_text(" in src
    # 两处子目录提示词注入点必须改走按章渲染（而非整案截断）
    assert "project_facts=_sublevel_facts_text([i])" in src
    assert "project_facts=_sublevel_facts_text(unit_idx)" in src


# ---------- E. R77b 终态汇总（accumulate / 载荷 / schema / 前端） ----------

def test_empty_summary_keys_match_contract():
    """空汇总键集与 FACT_AUTOFIX_SUMMARY_KEYS 逐字一致（防手写副本漂移）。"""
    from app.services.content_fact_autofix import (
        FACT_AUTOFIX_SUMMARY_KEYS,
        empty_fact_autofix_summary,
    )
    assert FACT_AUTOFIX_SUMMARY_KEYS == (
        "sections", "fixed_sections", "conflicts", "applied", "rejected")
    assert set(empty_fact_autofix_summary()) == set(FACT_AUTOFIX_SUMMARY_KEYS)
    assert all(v == 0 for v in empty_fact_autofix_summary().values())


def test_accumulate_counts_attempted_only():
    """只有 attempted 的报告进汇总；无冲突章节（无该键）原样返回。"""
    from app.services.content_fact_autofix import accumulate_fact_autofix
    base = empty = {"sections": 0, "fixed_sections": 0, "conflicts": 0,
                    "applied": 0, "rejected": 0}
    # 无 fact_autofix 键 / 非 dict / None → 一字不加
    for rep in ({"issues": []}, None, "x", {}):
        assert accumulate_fact_autofix(base, rep) == empty
    # attempted=False（无事实文本等早退）也不进汇总
    out = accumulate_fact_autofix(
        base, {"fact_autofix": {"attempted": False, "applied": 3}})
    assert out == empty


def test_accumulate_full_and_partial():
    """修复成功 / 尝试未成 / 部分拒绝三类形态的计数口径。"""
    from app.services.content_fact_autofix import accumulate_fact_autofix
    s0 = {"sections": 0, "fixed_sections": 0, "conflicts": 0,
          "applied": 0, "rejected": 0}
    s1 = accumulate_fact_autofix(s0, {"fact_autofix": {
        "attempted": True, "applied": 2, "rejected": [], "conflicts": 3,
        "attempts": 1}})
    assert s1 == {"sections": 1, "fixed_sections": 1, "conflicts": 3,
                  "applied": 2, "rejected": 0}
    # attempted 但 applied=0（AI 判定无冲突 / 定位失败）：sections+1、
    # fixed_sections+0、rejected 计入 —— 转人工信号不静默
    s2 = accumulate_fact_autofix(s1, {"fact_autofix": {
        "attempted": True, "applied": 0,
        "rejected": [{"reason": "ambiguous"}], "conflicts": 1, "attempts": 2}})
    assert s2 == {"sections": 2, "fixed_sections": 1, "conflicts": 4,
                  "applied": 2, "rejected": 1}
    # 不改入参（纯函数）
    assert s0 == {"sections": 0, "fixed_sections": 0, "conflicts": 0,
                  "applied": 0, "rejected": 0}
    # 坏形状汇总（None / 非 dict）按空汇总起步
    assert accumulate_fact_autofix(None, None)["sections"] == 0


def test_accumulate_tolerates_dirty_types():
    """脏值（字符串数字 / rejected 非列表）不得抛，按 0 计。"""
    from app.services.content_fact_autofix import accumulate_fact_autofix
    out = accumulate_fact_autofix({}, {"fact_autofix": {
        "attempted": True, "applied": "2", "conflicts": None,
        "rejected": "x"}})
    assert out == {"sections": 1, "fixed_sections": 1, "conflicts": 0,
                   "applied": 2, "rejected": 0}


def test_summary_wiring_in_sse_handlers():
    """终态汇总接线四要素：初始化 / 累加调用 / completed 载荷 / checkpoint。"""
    src = _src("app/routers/sse_handlers.py")
    assert "empty_fact_autofix_summary()" in src
    assert "accumulate_fact_autofix(_fa_sum, _std_rep)" in src
    assert "completed_payload['fact_autofix_summary'] = _fa_sum" in src
    assert "'fact_autofix_summary': _ckpt.get(\"fact_autofix_summary\", {})" in src
    # 白名单双副本（sse 侧 + schema 侧）必须同步 —— 漏一处则断线重挂丢字段
    assert '"fact_autofix_summary")),' in src
    schema = _src("app/services/checkpoint_schema.py")
    assert '"fact_autofix_summary",' in schema
    assert '"fact_autofix_summary": "dict",' in schema


def test_checkpoint_kinds_parity_and_ts_regenerated():
    """白名单两副本成员一致 + 前端 TS 已重新生成（R66 同款 parity 锁）。

    ⚠️ 锁**集合相等**而非逐字相等：两副本历史上就存在字段顺序差异
    （sse 侧 run_words/word_count 次序与 schema 侧相反，实测先于本轮存在），
    而消费方只做成员判定（过滤），顺序不参与任何判据 —— 逐字锁会把
    良性重排误判为漂移。新增/漏字段仍会被集合差抓住。
    """
    import app.routers.sse_handlers as sh
    from app.services import checkpoint_schema as cs
    sse_fields = sh._CHECKPOINT_KINDS["content_generation"][1]
    schema_fields = cs.CHECKPOINT_KINDS["content_generation"][1]
    assert "fact_autofix_summary" in sse_fields
    assert set(sse_fields) == set(schema_fields), "两副本白名单成员漂移"
    assert cs.CHECKPOINT_FIELD_TYPES["content_result"]["fact_autofix_summary"] == "dict"
    ts = _REPO / "frontend" / "src" / "types" / "checkpointSchema.ts"
    assert "fact_autofix_summary" in ts.read_text(encoding="utf-8")


def test_frontend_hint_pure_functions_present():
    """前端纯函数三件套（类型 / 归一 / 提示语）已落地且被两处消费。"""
    ce = _src(str(_REPO / "frontend" / "src" / "utils" / "contentEvents.ts"))
    assert "export function normalizeFactAutofixSummary(" in ce
    assert "export function factAutofixHint(" in ce
    assert "export type FactAutofixSummary" in ce
    for rel in ("frontend/src/hooks/useContentGeneration.ts",
                "frontend/src/pages/SchemeWorkbenchPage.tsx"):
        src = _src(str(_REPO / rel))
        assert "factAutofixHint(evt.fact_autofix_summary)" in src, (
            f"{rel} 未消费事实冲突修复汇总")


def test_fa_sum_rebinding_has_nonlocal():
    """闭包重绑定陷阱锁：gen_one 内重绑定累加器必须声明 nonlocal。

    ✅ R77b 实测缺陷（本轮自引入、全量 e2e 抓出）：`_fa_sum = accumulate(...)`
    是**名字重绑定**（accumulate 返回新 dict），未声明 nonlocal ⇒ Python 编译期
    把 `_fa_sum` 判为 `gen_one` 局部变量 ⇒ 每次 section_done 聚合块
    UnboundLocalError ⇒ section_done 事件下发失败（落库成功但前端收不到
    完成事件，`test_content_fence_contract` e2e 实测红）。
    ⚠️ 下标变异（`_std_sum[k] += 1`）不受此规则影响——所以 _std_sum 从来不炸，
    只有「重绑定型累加」需要本锁。AST 判据：gen_one 内凡 Name-Store 了
    累加器家族成员，就必须出现在 nonlocal 声明里。
    """
    import ast
    tree = ast.parse(_src("app/routers/sse_handlers.py"))

    def _find_fn(node, name):
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and n.name == name:
                return n
        return None

    gen_content = _find_fn(tree, "generate_content")
    assert gen_content is not None, "未找到 generate_content"
    gen_one = _find_fn(gen_content, "gen_one")
    assert gen_one is not None, "未找到 gen_one（嵌套于 generate_content）"

    declared: set[str] = set()
    for n in ast.walk(gen_one):
        if isinstance(n, ast.Nonlocal):
            declared.update(n.names)
    rebound = {n.id for n in ast.walk(gen_one)
               if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    # 累加器家族：闭包外层初始化 + 逐章更新。重绑定任一成员都必须 nonlocal。
    for acc in ("_std_sum", "_fa_sum", "_stat", "_prog", "_ckpt"):
        if acc in rebound:
            assert acc in declared, (
                f"gen_one 重绑定 {acc} 必须声明 nonlocal（闭包重绑定陷阱："
                "编译期判局部变量 → UnboundLocalError）")
    assert "_fa_sum" in rebound, "gen_one 内未见 _fa_sum 累加（接线被掏空？）"
    assert "_fa_sum" in declared


def test_outline_functions_importable_and_pure():
    """facts_builder 两个新函数存在且 relevant_first_rows 是纯函数。"""
    from app.services.facts_builder import (
        relevant_first_rows,
        render_outline_chapter_facts,
    )
    rows = [
        ("项目概况", "工程名称", "某某项目", 1.0, "overview"),
        ("监测方案", "预警值", "监测预警值 30mm", 1.0, "safety"),
        ("验收要求", "验收标准", "按规范验收", 1.0, "acceptance"),
    ]
    leaf = {"title": "安全监测", "description": "监测频率与预警值"}
    out = relevant_first_rows(rows, leaf)
    # 绝不丢事实：集合相等
    assert sorted(map(str, out)) == sorted(map(str, rows))
    # 相关事实（监测）与通用组（项目概况）进头部，验收垫后
    assert out[0][0] in ("项目概况", "监测方案")
    assert out[-1][0] == "验收要求"
    # 无关键词 → 原序等价
    assert relevant_first_rows(rows, {"title": "", "description": ""}) == rows
    # 渲染受预算约束
    text = render_outline_chapter_facts(rows, leaf, max_total=200)
    assert len(text) <= 200
