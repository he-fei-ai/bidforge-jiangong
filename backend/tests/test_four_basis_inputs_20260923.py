# -*- coding: utf-8 -*-
"""四项依据改造回归测试（2026-09-23）

覆盖：
1. scheme_scope：方案名称 → 主要施工内容抽取（复合名拆分、后缀剥离、回退、
   幂等与非法输入不抛异常）；
2. input_coverage：字段清单盘点（解析提取/全局事实/施工内容/编制要求）与
   调用对照差集分析（已调用/缺失/不可注入/截断）；
3. 提示词装配：outline_* / content_generation_system 模板含施工内容锚点与
   去AI化（允许适度口语化）纪律，渲染不残留未替换占位符；
4. GET /sse/input-coverage/{scheme_id} 端点函数（直调，不落真实库）。
"""
import json

import pytest

from app.services.scheme_scope import (extract_construction_scope,
                                       render_scope_for_prompt)
from app.services import input_coverage as ic
from app.services.ai.prompts._registry import (get_default_prompt,
                                               extract_variables)


# ===========================================================================
# 一、方案名称 → 主要施工内容
# ===========================================================================
class TestExtractConstructionScope:

    def test_compound_name_splits(self):
        """复合名称逐项拆分：每一项施工内容都是章节划分依据"""
        assert extract_construction_scope("基坑支护及土方开挖专项施工方案") == \
            ["基坑支护", "土方开挖"]
        assert extract_construction_scope("高支模与脚手架工程安全专项施工方案") == \
            ["高支模", "脚手架"]
        assert extract_construction_scope("塔吊安拆、使用与维护专项方案") == \
            ["塔吊安拆", "使用", "维护"]

    def test_single_content_falls_back_to_core(self):
        """单内容方案：返回名称主体（与旧「整名注入」信息等价，不编造）"""
        assert extract_construction_scope("附着式升降脚手架工程专项施工方案") == \
            ["附着式升降脚手架"]
        assert extract_construction_scope("临时用电施工组织设计") == ["临时用电"]

    def test_suffix_stripping_loop(self):
        """多重后缀（安全专项施工方案）循环剥离"""
        core = extract_construction_scope("深基坑工程安全专项施工方案")
        assert core == ["深基坑"]

    def test_empty_and_illegal_no_raise(self):
        """非法输入不抛异常（纯函数约定）"""
        assert extract_construction_scope("") == []
        assert extract_construction_scope(None) == []
        assert extract_construction_scope(123) == []
        assert extract_construction_scope("专项施工方案")  # 整名即后缀：回退不崩

    def test_render_scope_line(self):
        assert render_scope_for_prompt("基坑支护及土方开挖专项施工方案") == \
            "基坑支护、土方开挖"
        # 拆不出时回退名称主体；空名给显式占位（不得诱导模型编造）
        assert render_scope_for_prompt("某某工程") == "某某工程"
        assert "未提供" in render_scope_for_prompt("")

    def test_dedup_keeps_order(self):
        """重复片段去重且保持首现顺序"""
        assert extract_construction_scope("土方开挖及土方开挖") == ["土方开挖"]


# ===========================================================================
# 二、字段清单与调用对照（差集分析）
# ===========================================================================
async def _seed_inputs(db_conn, project_id="p1", scheme_id="s1"):
    """落一组最小输入：2 项提取（1 有效 1 未提取到）+ 3 组事实 + 施工内容。"""
    await db_conn.execute(
        "INSERT INTO bid_analysis_items(id, project_id, item_id, label, "
        "output_type, status, content, sort_order) VALUES "
        "('a','p1','projectBasicInfo','项目级基本信息','json','success',"
        "'{\"基坑深度\": \"8.5m\"}',1),"
        "('b','p1','overviewParams','工程概况与设计参数','markdown','success',"
        "'未提取到',2),"
        "('c','p1','safetyMeasures','安全保证措施','markdown','success',"
        "'坑边荷载不得超过设计限值',3)")
    # 事实：已确认可注入 / 未裁决 pending / 矛盾 conflict
    await db_conn.execute(
        "INSERT INTO global_facts(id, project_id, scheme_id, group_title, "
        "title, content, is_resolved, has_conflict) VALUES "
        "('f1','p1','s1','工程概况','基坑深度','8.5m',1,0),"
        "('f2','p1','s1','地质条件','地下水位','待复核',0,0),"
        "('f3','p1','s1','地质条件','支护形式','两值矛盾',1,1)")
    await db_conn.commit()


@pytest.mark.asyncio
async def test_build_inventory_statuses(db_conn):
    """清单状态：有效/未提取到/可注入/被过滤（矛盾、未确认）逐一显式台账"""
    await _seed_inputs(db_conn)
    inv = await ic.build_inventory(db_conn, "p1", "s1",
                                   scope_items=["基坑支护", "土方开挖"],
                                   requirements_text="必须包含监测方案")
    parse = {e.key: e for e in inv.by_source(ic.SRC_PARSE)}
    assert parse["projectBasicInfo"].status == "available"
    assert parse["overviewParams"].status == "empty"        # 未提取到
    facts = inv.by_source(ic.SRC_FACTS)
    # 可注入组 + 两个被过滤组（未确认 / 矛盾），分组聚合、状态可辨
    assert any(e.status == "available" and e.label == "工程概况" for e in facts)
    assert any(e.status == "filtered_unresolved" and e.label == "地质条件" for e in facts)
    assert any(e.status == "filtered_conflict" and e.label == "地质条件" for e in facts)
    assert len(inv.by_source(ic.SRC_SCOPE)) == 2
    assert len(inv.by_source(ic.SRC_REQ)) == 1


@pytest.mark.asyncio
async def test_audit_prompt_coverage_diff(db_conn):
    """差集分析：装配文本出现锚点 → 已调用；未出现 → 缺失；截断源透传"""
    await _seed_inputs(db_conn)
    inv = await ic.build_inventory(db_conn, "p1", "s1",
                                   scope_items=["基坑支护", "土方开挖"],
                                   requirements_text="必须包含监测方案")
    prompt = ("# 提取项目结果\n\n## 项目级基本信息\n- **基坑深度**: 8.5m\n"
              "\n## 安全保证措施\n…\n\n### 工程概况\n8.5m\n"
              "【方案名称主要施工内容】：基坑支护、土方开挖\n"
              "【编制要求…】：必须包含监测方案\n")
    report = ic.audit_prompt_coverage(inv, prompt, {ic.SRC_PARSE: "预算截断"})
    injected_labels = {r["label"] for r in report["injected"]}
    assert "项目级基本信息" in injected_labels
    assert "工程概况" in injected_labels
    assert "基坑支护" in injected_labels
    missing_labels = {r["label"] for r in report["missing"]}
    assert "土方开挖" not in missing_labels        # 已出现 → 不缺失
    # 「未提取到」项与两类被过滤事实进入 not_injectable（不冤枉为缺失）
    ni = {r["key"] for r in report["not_injectable"]}
    assert "overviewParams" in ni
    assert any("filtered_conflict" in k for k in ni)
    assert report["truncated"] == [{"source": ic.SRC_PARSE, "reason": "预算截断"}]
    assert "缺失" in report["summary"]


@pytest.mark.asyncio
async def test_audit_missing_field_detected(db_conn):
    """反例：某提取项成功但没进提示词 → 必须被判为缺失（防静默丢失）"""
    await _seed_inputs(db_conn)
    inv = await ic.build_inventory(db_conn, "p1", "s1", scope_items=[], requirements_text="")
    report = ic.audit_prompt_coverage(inv, "只提到 项目级基本信息：\n## 项目级基本信息\n")
    missing = {r["key"] for r in report["missing"]}
    assert "safetyMeasures" in missing             # 有效成果未被调用
    assert "projectBasicInfo" not in missing


def test_log_coverage_audit_never_raises():
    """审计日志函数吞掉一切异常（不得阻断生成主流程）"""
    ic.log_coverage_audit("outline", "s1", {"missing": [], "truncated": [],
                                            "summary": "ok"})
    ic.log_coverage_audit("outline", "s1", None)   # 非法入参也不抛


# ===========================================================================
# 三、提示词模板：施工内容锚点 + 去AI化/适度口语化
# ===========================================================================
OUTLINE_KEYS = ["outline_short_system", "outline_level1_system",
                "outline_sublevel_system", "outline_sublevel_batch_system",
                "outline_review_system"]


@pytest.mark.parametrize("key", OUTLINE_KEYS)
def test_outline_templates_have_scope_anchor(key):
    """目录各模板含 {construction_scope} 锚点与四项依据纪律"""
    tpl = get_default_prompt(key)
    assert "{construction_scope}" in tpl, key
    assert "主要施工内容" in tpl, key


def test_outline_short_basis_declaration():
    """短方案模板明确四项依据（解析提取/施工内容/全局事实/目录库参考）"""
    tpl = get_default_prompt("outline_short_system")
    assert "四项输入" in tpl or "四项依据" in tpl
    assert "不得虚构" in tpl


def test_content_template_deai_and_colloquial():
    """正文模板：去AI化红线保留，同时允许适度口语化平实表达"""
    tpl = get_default_prompt("content_generation_system")
    assert "语言规范（去AI化，硬性）" in tpl
    assert "适度口语化" in tpl
    assert "排比堆砌" in tpl and "空话套话" in tpl
    assert "【方案名称主要施工内容】" in tpl   # 内容展开依据条款
    # 旧「不得出现口语化」一刀切表述已移除
    assert "不得出现口语化" not in tpl


def test_content_scope_line_renders():
    """施工内容文本渲染入提示词不残留占位符（锚点名与模板一致）"""
    tpl = get_default_prompt("outline_level1_system")
    kwargs = {v: "X" for v in extract_variables(tpl)}
    from app.services.ai.prompts._registry import render_prompt
    out = render_prompt(tpl, **kwargs)
    assert "{construction_scope}" not in out


# ===========================================================================
# 四、差集审计端点（直调函数）
# ===========================================================================
@pytest.mark.asyncio
async def test_input_coverage_endpoint(db_conn):
    from app.routers.sse_handlers import input_coverage_snapshot
    await _seed_inputs(db_conn)
    await db_conn.execute(
        "INSERT INTO schemes(id, project_id, name, type, config_json) "
        "VALUES('s1','p1','基坑支护及土方开挖专项施工方案','深基坑',?)",
        (json.dumps({"requirements": "必须包含监测方案"}),))
    await db_conn.commit()
    data = await input_coverage_snapshot("s1", db=db_conn)
    assert data["construction_scope"]["items"] == ["基坑支护", "土方开挖"]
    assert data["summary"]["by_source"][ic.SRC_PARSE]["available"] == 2
    # 未提取到项 available 计数不含 → 台账可见
    assert data["summary"]["available"] < data["summary"]["total"]
    with pytest.raises(Exception):
        await input_coverage_snapshot("nope", db=db_conn)


# ===========================================================================
# 五、代码审查修复回归（2026-09-23）：M2 / M3 / m1 / m2
# ===========================================================================
@pytest.mark.asyncio
async def test_inventory_non_success_not_injectable(db_conn):
    """M2：解析提取非 success 行（running/error 保留旧 content）不得判
    available，必须落入 not_injectable，避免每轮生成虚假「缺失」告警。"""
    await db_conn.execute(
        "INSERT INTO bid_analysis_items(id, project_id, item_id, label, "
        "output_type, status, content, sort_order) VALUES "
        "('x','p1','stuckItem','失败的提取项','markdown','error','旧残留内容',1),"
        "('y','p1','runItem','在途的提取项','markdown','running','上一轮内容',2),"
        "('z','p1','okItem','正常提取项','markdown','success','有效内容',3)")
    await db_conn.commit()
    inv = await ic.build_inventory(db_conn, "p1", "s1",
                                   scope_items=[], requirements_text="")
    parse = {e.key: e for e in inv.by_source(ic.SRC_PARSE)}
    assert parse["stuckItem"].status == "not_success"
    assert parse["runItem"].status == "not_success"
    assert parse["okItem"].status == "available"
    # 差集：非 success 项进 not_injectable，不被误判为「缺失」
    report = ic.audit_prompt_coverage(inv, "## 正常提取项\n")
    missing = {r["key"] for r in report["missing"]}
    assert "stuckItem" not in missing and "runItem" not in missing
    ni = {r["key"] for r in report["not_injectable"]}
    assert "stuckItem" in ni and "runItem" in ni


def test_site_trunc_extra_uses_actual_budget():
    """M3：截断台账按装配点实际预算入账，未超预算零记录。"""
    from app.routers.sse_handlers import _site_trunc_extra
    assert _site_trunc_extra("解析提取", "全局事实", "短文本", 3000) == {}
    extra = _site_trunc_extra("解析提取", "全局事实",
                              "x" * 3500, 2000, "f" * 1600, 1500)
    assert extra["解析提取"] == "项目资料摘要 3500 字超本装配点预算 2000 字，已截断"
    assert extra["全局事实"] == "全局事实 1600 字超本装配点预算 1500 字，已截断"


@pytest.mark.asyncio
async def test_audit_accepts_callable_and_warns_on_failure(db_conn, caplog):
    """m1+m2：审计接受 callable 样本；样本求值抛错时吞异常并打 WARNING（不阻断、生产可见）。"""
    import logging
    from app.routers.sse_handlers import _run_input_coverage_audit
    await _seed_inputs(db_conn)
    # callable 正常路径：不抛
    # ✅ 2026-09-26：scene 由 "content"（已从 KNOWN_SCENES 移除的僵尸总调度
    #    场景）改为真实调用点 "content_draft"。此处只是 mock 参数，但保留一个
    #    未登记的 scene 会让双向护栏 test_all_used_scenes_registered 误报。
    await _run_input_coverage_audit(
        db_conn, "p1", "s1", ["基坑支护"], "必须含监测",
        lambda: "## 项目级基本信息\n### 工程概况\n编制要求", scene="content_draft")

    # 样本求值抛错：审计内部吞掉，以 WARNING 曝露（不再只落 DEBUG）
    def _boom():
        raise RuntimeError("行序变化导致渲染失败")
    with caplog.at_level(logging.WARNING, logger="sse"):
        await _run_input_coverage_audit(
            db_conn, "p1", "s1", [], "", _boom, scene="content_draft")
    assert any("差集审计失败" in r.getMessage() for r in caplog.records)


# ===========================================================================
# 六、下轮 Minor 修复回归：m3（降级可见）/ m4（边界截断）
# ===========================================================================
class _BoomDB:
    """任意 execute 均抛错，用子验证 build_inventory 降级路径。"""
    async def execute(self, *a, **k):
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_build_inventory_marks_degraded_on_failure():
    """m3：数据源查询异常时登记降级源（两个 DB 源），非 DB 的施工内容仍入账。"""
    inv = await ic.build_inventory(_BoomDB(), "p1", "s1",
                                   scope_items=["基坑支护"], requirements_text="")
    assert set(inv.degraded) == {ic.SRC_PARSE, ic.SRC_FACTS}
    assert len(inv.by_source(ic.SRC_SCOPE)) == 1


def test_audit_degraded_logs_warning_not_green(caplog):
    """m3：无缺失/无截断但有降级源时，仍必须报 WARNING（不能假全绿 INFO）。"""
    import logging
    inv = ic.InputInventory(
        entries=[ic.FieldEntry(source=ic.SRC_PARSE, key="k",
                               label="项目级基本信息", status="available", chars=5)],
        degraded=[ic.SRC_PARSE])
    rep = ic.audit_prompt_coverage(inv, "## 项目级基本信息\n")
    assert rep["degraded"] == [ic.SRC_PARSE]
    assert "降级源" in rep["summary"]
    assert not rep["missing"] and not rep["truncated"]   # 否则无法证明“仅降级”触发
    with caplog.at_level(logging.INFO, logger="input_coverage"):
        ic.log_coverage_audit("outline", "s1", rep)
    assert any(r.levelno >= logging.WARNING for r in caplog.records), \
        "降级源必须抬升为 WARNING，避免与“全绿”混淡"


def test_build_structured_brief_uses_boundary_truncation():
    """m4：结构化摘要改用边界感知截断，不再硬切小节/围栏，并去除 no-op 外层切片。"""
    import inspect
    from app.routers.sse_handlers import _build_structured_brief
    src = inspect.getsource(_build_structured_brief)
    assert "truncate_to_boundary(structured, max_chars)" in src
    assert "[:max_chars + 900]" not in src
