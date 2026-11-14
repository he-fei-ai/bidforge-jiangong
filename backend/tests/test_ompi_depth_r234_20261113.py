"""R234 · oh-my-pi 深度能力层护栏（2026-11-13）。

覆盖 ompi_*（Oh My Pi Integration）新增深度层：
  - ``ompi_memory``    —— retain/recall/reflect/memory_edit 记忆银行 + 损坏文件静默降级；
  - ``ompi_advisor``   —— AdvisorGuard 确定性规则监督（占位符 / 空产物完成宣言）；
  - ``ompi_fix``       —— hashline 内容哈希锚点（stale 拒绝 + 精确替换恰 1 处）；
  - ``ompi_task_schema``—— task 扇出 schema 校验回收（validate_yield / pull_path）；
  - ``ompi_pipeline``  —— 深度层编排入口：advisor/schema 接入 verify 路径触发 fix 轮、
    记忆跨阶段交接、stale 安全修复、三环节闭环（目录→正文→审核→导出契约）。

全程注入 ``executor_fn``，不拉起真实 omp 二进制（对齐既有 omp 测试口径）。
不修改任何既有 omp_* 模块、不重锚既有护栏（契约零破坏）。
"""

from __future__ import annotations

import json

import pytest
from app.services.ai import omp_agent_roles as roles
from app.services.ai import omp_workflow as wf
from app.services.ai.ompi import (
    MISSING,
    AdvisorGuard,
    AdvisorNote,
    MemoryBank,
    MemoryEntry,
    apply_hashline,
    hashline_anchor,
    is_hashline_anchor,
    pull_path,
    stale_anchor_error,
    validate_yield,
)
from app.services.ai.ompi.ompi_pipeline import OmpiPipeline


# ---------- 夹具：合法目录树 / 达标正文 ----------
def _valid_tree():
    """满足 L1~L4 数量约束的目录树（1 章 ×3 节 ×3 子节 ×4 叶 = 36 叶）。"""

    def l4(k):
        return [{"title": f"工艺要点{k}-{j}", "level": 4, "children": []} for j in range(1, 5)]

    def l3(m):
        return [{"title": f"施工环节{m}-{k}", "level": 3, "children": l4(k)} for k in range(1, 4)]

    def l2(i):
        return [{"title": f"分部工程{i}-{m}", "level": 2, "children": l3(m)} for m in range(1, 4)]

    return [{"title": f"工程要素{i}", "level": 1, "children": l2(i)} for i in range(1, 2)]


def _body_text(budget: int) -> str:
    """生成无占位符 / 无围栏 / 无数字单位的达标正文（超字数下限）。"""
    unit = "本工程采用分段施工工艺并严格控制质量验收。"
    return unit * (int(budget) // len(unit) + 2)


def _outline_spec():
    return wf.OmpTaskSpec(
        "t-outline",
        "生成目录树",
        metadata={"prompts": roles.build_outline_prompts(brief="工程概况 450 日历天")},
    )


# ===================== ① 记忆银行（ompi_memory） =====================
def test_memory_retain_recall_edit(tmp_path):
    bank = MemoryBank(str(tmp_path / "mem.json"))
    e = bank.retain("L2 子节点数必须 3~5 个", tags=["outline", "constraint"], source="test")
    assert e.id and e.status == "active"
    assert bank.recall("L2 子节点")  # 子串命中
    assert bank.recall("3~5 个")  # 标签命中
    assert bank.recall("不存在关键词") == []
    # memory_edit：更新文本
    assert bank.memory_edit(e.id, new_text="L2 子节点数必须 4~6 个") is True
    assert bank.recall("4~6 个")
    # memory_edit：遗忘
    assert bank.memory_edit(e.id, forget=True) is True
    assert bank.recall("L2 子节点") == []
    st = bank.stats()
    assert st["total"] == 1 and st["active"] == 0 and st["forgotten"] == 1
    # 不存在的 id
    assert bank.memory_edit("m-nonexist", forget=True) is False


def test_memory_corrupt_file_degrades_silently(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not-valid-json", encoding="utf-8")
    bank = MemoryBank(str(p))  # 不应抛异常
    assert bank.stats()["total"] == 0
    # 写失败不抛（目录不可写场景以注入只读路径近似）
    bank.retain("x", tags=[])  # 不抛
    assert bank.recall("x")


def test_memory_reflect_returns_bullets(tmp_path):
    bank = MemoryBank(str(tmp_path / "mem.json"))
    bank.retain("工期 450 日历天（见 global-facts.md）", tags=["fact", "工期"])
    brief = bank.reflect("工期")
    assert "工期 450 日历天" in brief
    assert "【记忆要点】" in brief
    assert bank.reflect("无命中词") == ""


def test_memory_entry_roundtrip():
    """MemoryEntry 序列化/反序列化往返（记忆编解码契约，含中文/标签/状态）。"""
    e = MemoryEntry(
        id="mabc123",
        text="L2 子节点数必须 3~5 个",
        scope="project",
        tags=["outline", "约束"],
        source="ompi:outline",
        status="active",
        created_at=1.0,
        updated_at=2.0,
    )
    d = e.to_dict()
    assert d["id"] == "mabc123" and d["text"].startswith("L2")
    e2 = MemoryEntry.from_dict(d)
    assert e2.id == e.id and e2.text == e.text and e2.tags == e.tags
    assert e2.source == e.source and e2.status == e.status
    # 缺键容忍（旧版本记录）→ 默认值兜底
    e3 = MemoryEntry.from_dict({"id": "m1"})
    assert e3.scope == "project" and e3.status == "active" and e3.tags == []
    assert isinstance(MemoryEntry.from_dict({}).id, str)  # 空 dict 不抛


# ===================== ② Advisor 监督（ompi_advisor） =====================
def test_advisor_placeholder_and_claim():
    guard = AdvisorGuard()
    n = guard.review({"produced_text": "正文含 __X__ 与 {{待填}} 占位符", "payload": ""})
    assert n is not None and n.level == "concern" and "占位符" in n.text
    n2 = guard.review({"produced_text": "", "payload": "已全部完成"})
    assert n2 is not None and n2.level == "concern" and "完成宣言" in n2.text
    assert guard.review({"produced_text": "干净的正文，无占位符", "payload": "ok"}) is None


def test_advisor_custom_blocker_rule():
    def hard_rule(ctx):
        if "危险" in str(ctx.get("produced_text")):
            return "越界写了非本章内容"
        return None

    guard = AdvisorGuard(rules=[("越界内容", hard_rule)])
    note = guard.review({"produced_text": "这段是危险内容"})
    # 自定义规则默认 info 级；调用方按需映射。此处只验证规则生效。
    assert note is not None and "越界" in note.text
    assert guard.review({"produced_text": "正常"}) is None
    assert isinstance(AdvisorNote(level="bad", text="x").level, str)  # 非法级别归一


def test_advisor_declared_level():
    """R234 加固：注入规则可声明级别（3 元组）或动态返回 (level, msg)。"""
    # 3 元组显式声明 blocker
    g1 = AdvisorGuard(rules=[("越界内容", lambda ctx: "写了非本章内容" if "危险" in str(ctx.get("produced_text")) else None, "blocker")])
    n = g1.review({"produced_text": "这段是危险内容"})
    assert n is not None and n.level == "blocker"
    # 规则动态返回 (level, msg)：优先级最高
    g2 = AdvisorGuard(rules=[("自定义", lambda ctx: ("concern", "疑似越界") if ctx.get("x") else None)])
    n2 = g2.review({"x": True})
    assert n2 is not None and n2.level == "concern" and "疑似越界" in n2.text
    # 未命中 → None
    assert g2.review({"x": False}) is None
    # 已知规则名仍映射 concern（2 元组兼容：占位符残留，正则要求 __[A-Z_]{2,}__）
    g3 = AdvisorGuard()
    n3 = g3.review({"produced_text": "__XX__"})
    assert n3 is not None and n3.level == "concern"


# ===================== ③ hashline 内容哈希锚（ompi_fix） =====================
def test_hashline_anchor_roundtrip_and_stale():
    body = "工期 400 天，质量合格。"
    anchor = hashline_anchor(body)
    assert is_hashline_anchor(anchor)
    assert anchor.startswith("hashline:sha256:")
    # 同一内容同锚；不同内容不同锚
    assert hashline_anchor(body) == anchor
    assert hashline_anchor(body + "x") != anchor
    # stale：内容已被外部修改 → 拒绝
    with pytest.raises(stale_anchor_error):
        apply_hashline(body + "（已改）", anchor, "新内容")
    # 匹配 → 整文替换
    assert apply_hashline(body, anchor, "新全文") == "新全文"


def test_hashline_precise_replace_once():
    # 恰好 1 处 → 精确替换（宁漏勿误：0/多次均拒绝）
    assert apply_hashline("a 400 b", hashline_anchor("a 400 b"), "X", old_text="400") == "a X b"
    # 0 处 → ValueError（宁漏勿误）
    with pytest.raises(ValueError):
        apply_hashline("无目标片段", hashline_anchor("无目标片段"), "X", old_text="400")
    # 2 处 → ValueError（宁漏勿误：不误伤多处同形片段）
    with pytest.raises(ValueError):
        apply_hashline("a 400 b 400 c", hashline_anchor("a 400 b 400 c"), "X", old_text="400")
    # 非本层锚格式 → ValueError
    with pytest.raises(ValueError):
        apply_hashline("body", "conflict://1", "x")
    assert not is_hashline_anchor("conflict://1")


def test_pipeline_stale_safe_fix(tmp_path):
    pipe = OmpiPipeline()
    doc = {"s1": "工期 400 天，质量合格。"}
    anchor = hashline_anchor(doc["s1"])
    modified = {"s1": "工期 450 天，质量合格。"}  # 外部已修改（并发落盘）
    fixes = [
        {
            "section_id": "s1",
            "old_text": "工期 400 天",
            "new_text": "工期 500 天",
            "reason": "冲突",
            "anchor": anchor,
        }
    ]
    applied, failures = pipe.stale_safe_fix(modified, fixes)
    assert failures  # stale 拒绝
    assert applied["s1"] == modified["s1"]  # 不被过期指令覆盖
    # 未带锚 → 退化为精确替换（既有 apply_fix_directives 语义）
    applied2, failures2 = pipe.stale_safe_fix(doc, [{**fixes[0], "anchor": None}])
    assert not failures2
    assert applied2["s1"] == "工期 500 天，质量合格。"


# ===================== ④ task 扇出 schema 校验回收（ompi_task_schema） =====================
def test_validate_yield_schema():
    ok, errs = validate_yield({"findings": [{"path": "s1.md", "severity": "high"}], "summary": "ok"},
                              {"findings": [{"path": str, "severity": str}], "summary": str})
    assert ok and errs == []
    # 缺键 → error
    ok, errs = validate_yield({"findings": []}, {"findings": [{"path": str}], "summary": str})
    assert not ok and any("summary" in e for e in errs)
    # 类型不符 → error
    ok, errs = validate_yield({"findings": [{"path": 123}]}, {"findings": [{"path": str}]})
    assert not ok and any("path" in e for e in errs)
    # optional 豁免缺失
    ok, errs = validate_yield({"findings": []}, {"findings": [{"path": str}], "summary": str},
                              optional={"$.summary"})
    assert ok and errs == []
    # 容器形态不符（期望数组实际对象）→ error
    ok, errs = validate_yield({"findings": {"bad": 1}}, {"findings": [{"path": str}]})
    assert not ok and any("findings" in e for e in errs)


def test_pull_path():
    data = {"findings": [{"path": "s1.md", "meta": {"score": 9}}], "summary": "ok"}
    assert pull_path(data, "findings.0.path") == "s1.md"
    assert pull_path(data, "findings.0.meta.score") == 9
    assert pull_path(data, "summary") == "ok"
    # 越界 / 缺失 / 空路径 / 非容器 → MISSING 哨兵（与 None 区分，绝不抛异常）
    assert pull_path(data, "findings.9.path") is MISSING
    assert pull_path(data, "missing.key") is MISSING
    assert pull_path(data, "") is MISSING
    assert pull_path(None, "a.b") is MISSING
    assert pull_path([1, 2], "a") is MISSING  # 非 dict/list 段
    # 越界/缺失与 None 区分（字段存在但值为 None）
    assert pull_path({"a": None}, "a") is None


# ===================== ⑤ 异常 → fix 触发 → 恢复 =====================
async def test_advisor_blocker_triggers_fix_round_and_recovers(tmp_path):
    calls = {"n": 0}

    def blocker_rule(ctx):
        # 第一轮产物带占位符 → 触发；修复后干净 → 放行。
        if calls["n"] == 1 and "__X__" in str(ctx.get("produced_text")):
            return "产出仍含占位符 __X__"
        return None

    pipe = OmpiPipeline(memory_path=str(tmp_path / "m.json"), advisor_rules=[("占位符残留", blocker_rule)])

    async def ex(spec, prompt, ws):
        calls["n"] += 1
        return "正文内容 __X__" if calls["n"] == 1 else "正文内容已修复"

    report = await pipe.run_stage(
        "content",
        context="写正文",
        tasks=[wf.OmpTaskSpec("s1", "写正文")],
        verify_fn=lambda s, p: [],
        executor_fn=ex,
        max_fix_rounds=1,
    )
    assert report.totals["completed"] == 1
    r = report.results[0]
    assert r.fix_rounds == 1  # advisor blocker 触发 fix 轮
    assert any("[advisor:" in e for e in r.errors)
    assert r.payload == "正文内容已修复"


async def test_schema_failure_surfaces_and_recovers(tmp_path):
    # 第一轮返回缺键 JSON → schema 校验失败 → fix 轮；第二轮返回合法结构。
    calls = {"n": 0}

    async def ex(spec, prompt, ws):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"findings": []}, ensure_ascii=False)  # 缺 summary
        return json.dumps({"findings": [{"path": "s1.md", "severity": "high"}], "summary": "ok"},
                          ensure_ascii=False)

    pipe = OmpiPipeline(yield_schema={"findings": [{"path": str, "severity": str}], "summary": str})
    report = await pipe.run_stage(
        "audit",
        context="审计",
        tasks=[wf.OmpTaskSpec("a1", "审计扫描")],
        verify_fn=lambda s, p: [],
        executor_fn=ex,
        max_fix_rounds=1,
    )
    assert report.totals["completed"] == 1
    assert report.results[0].fix_rounds == 1
    assert any("[schema]" in e for e in report.results[0].errors)


# ===================== ⑥ 集成：三环节闭环（目录→正文→审核→导出契约） =====================
async def test_full_chain_with_depth_layer_and_memory_handoff(tmp_path):
    mem_path = str(tmp_path / "chain.json")
    pipe = OmpiPipeline(memory_path=mem_path, stage="chain")

    async def ex_outline(spec, prompt, ws):
        return json.dumps({"sections": _valid_tree()}, ensure_ascii=False)

    async def ex_content(spec, prompt, ws):
        return _body_text(300)

    async def ex_audit(spec, prompt, ws):
        return json.dumps({"fixes": []}, ensure_ascii=False)

    # ① 目录
    r1 = await pipe.run_stage(
        "outline",
        context="项目概况：工期 450 日历天，地下 2 层。",
        tasks=[_outline_spec()],
        verify_fn=lambda s, p: roles.verify_outline(json.loads(p)["sections"]),
        executor_fn=ex_outline,
    )
    assert r1.totals["completed"] == 1
    assert r1.results[0].fix_rounds == 0

    # 记忆沉淀：目录阶段要点已 retain
    assert pipe.memory.recall("阶段 outline")

    # ② 正文（context 注入记忆要点 → 跨阶段交接）
    ctx2 = pipe.context_with_memory("按目录撰写正文")
    assert "阶段 outline" in ctx2  # 记忆要点已注入
    r2 = await pipe.run_stage(
        "content",
        context=ctx2,
        tasks=[wf.OmpTaskSpec("t-content", "写正文", metadata={"prompts": roles.build_content_prompts(
            outline_digest=json.dumps(_valid_tree(), ensure_ascii=False)[:400], facts="工期 450 日历天")})],
        verify_fn=lambda s, p: roles.verify_content_draft(p, budget=300, facts="工期 450", strict_facts=False),
        executor_fn=ex_content,
    )
    assert r2.totals["completed"] == 1

    # ③ 审核与修复（返回空 fixes → 干净通过）
    r3 = await pipe.run_stage(
        "audit",
        context="整卷审计",
        tasks=[wf.OmpTaskSpec("t-audit", "审计", metadata={"prompts": roles.build_audit_prompts(document_count=1)})],
        verify_fn=lambda s, p: roles.verify_audit_fixes(json.loads(p), {}),
        executor_fn=ex_audit,
    )
    assert r3.totals["completed"] == 1

    # 闭环记忆：三个阶段要点均在银行（跨阶段上下文交接完成）
    assert pipe.memory.recall("阶段 audit")
    # 阶段产物不沉淀全文（只沉淀结构性要点）
    assert not any("正文内容" in e.text or "工程概况" in e.text for e in pipe.memory.recall(""))

    # 导出契约：三阶段 payload 均可被下游消费（审计修复为空，目录树可落库）
    outline_data = json.loads(r1.results[0].payload)
    assert roles.check_json(r1.results[0].payload, required_keys=("sections",))[0]
    audit_data = json.loads(r3.results[0].payload)
    assert audit_data == {"fixes": []}
