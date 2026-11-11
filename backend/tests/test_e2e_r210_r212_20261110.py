"""R210-R212 三模块端到端贯通护栏（全局事实 → 正文生成 → 审核预检 → 导出）

背景
----
用户在同一条指令链中连续要求三轮深度审计（R210 全局事实 / R211 正文生成 /
R212 审核与预检）。三轮各自有独立护栏，但**跨模块的贯通链**此前没有任何一条
护栏覆盖：三个模块各自全绿，并不等于「事实 → 正文 → 审核 → 导出」这条链
真的通。

本文件只锁**跨模块贯通意图**，不重复各模块自己的护栏（那些在
``test_global_facts_r210_20261110.py`` / ``test_content_r211_20261110.py`` /
``test_review_r212_20261110.py``）。锁的五条链：

  CHAIN-1 事实 → 预检危大：已确认事实必须经**同一注入门控**抵达预检，
          并让「已确认事实参数达到危大阈值」被真实判出（漏判是安全红线）。
  CHAIN-2 事实过期 → 全链 fail-closed：来源过期的事实在**正文/导出/预检**
          三处同时被排除 —— 门控口径不得在任何一处放宽。
  CHAIN-3 事实过期 → 复核清单可见：被门控排除的事实必须同时出现在
          「待确认」清单里，否则事实从交付物里静默消失而用户零感知。
  CHAIN-4 正文规范化 → 落库字数 → 预检/导出字数口径：净化型改写也必须
          被判为「正文变了」，否则落库字数与正文不一致，续写/压缩触发随之
          判错；低字数阈值在预检与导出两侧必须同源。
  CHAIN-5 章节 id 贯通：预检 finding 必须带**真实 section_id**（否则前端
          定位跳转不可达、修复侧按标题反查会命中多章）。

禁改原则：断言锁的是**贯通意图**（门控同源、过期可见、正文变了、id 可定位），
不是实现细节；重构实现时请同步更新断言，但不得删断言。
"""

from __future__ import annotations

import json
import uuid

import app.routers.export as export_router
import app.services.audit_rules as ar
import app.services.audit_scoring as asc
from app.services import dossier
from app.services import preflight_engine as pe
from app.services.content_sanitize import normalize_raw_content
from app.services.facts_extractor import load_resolved_facts_for_scope
from app.services.numbering import content_actually_changed

# ===========================================================================
# 公共夹具
# ===========================================================================


async def _seed_scheme(db) -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    return pid, sid


async def _seed_fact(db, pid, sid, fid, name, value, *, is_resolved=1, is_stale=0):
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
        " title, content, category, source_ref, is_simulated, confidence, is_resolved,"
        " has_conflict, conflict_keys, fact_key, is_stale)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            pid,
            sid,
            "g_" + fid,
            name,
            name,
            f"- **{name}**: {value}",
            "tech_param",
            json.dumps([{"file": "招标文件", "quote": "x"}], ensure_ascii=False),
            0,
            0.9,
            is_resolved,
            0,
            "",
            name,
            is_stale,
        ),
    )
    await db.commit()


def _hazard_ctx(scheme_id: str, facts: list) -> pe.PreflightContext:
    """装配「名称/类型不含危大关键词」的预检上下文。

    名称必须**不含**危大关键词：预检的 SAF-08 只在「关键词口径未判危大」
    时才给出事实反哺结论（否则无信息增量），否则本护栏会假绿。
    """
    return pe.PreflightContext(
        scheme_id=scheme_id,
        scheme_name="某专项施工方案",
        scheme_type="",
        sections=[{"id": "sec-1", "title": "工程概况", "content": "正文内容", "word_count": 100, "parent_id": ""}],
        facts=facts,
    )


# ===========================================================================
# CHAIN-1 · 事实 → 预检危大判定（数据链真实抵达）
# ===========================================================================


async def test_confirmed_fact_reaches_preflight_hazard_judgement(db_conn):
    """已确认的危大参数事实必须真实判出 SAF-08（漏判是安全红线）。

    链路：DB → ``load_resolved_facts_for_scope``（注入门控）→ ``ctx.facts``
    → ``check_hazard_params`` → finding。若中间任一环断链，本用例失败。
    """
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f-depth", "基坑开挖深度", "16m", is_resolved=1, is_stale=0)

    facts = await load_resolved_facts_for_scope(db_conn, sid)
    assert facts, "已确认事实必须能经注入门控抵达预检桥接"
    assert any(f.get("name") == "基坑开挖深度" for f in facts), facts

    findings = pe.check_hazard_params(_hazard_ctx(sid, facts))
    assert any(f.get("rule_id") == "SAF-08" for f in findings), findings


# ===========================================================================
# CHAIN-2 · 事实过期 → 正文/导出/预检三处同时 fail-closed
# ===========================================================================


async def test_stale_fact_is_excluded_from_every_downstream_gate(db_conn):
    """来源过期的事实在注入门控侧被排除 ⇒ 预检不得再用它判危大。

    注入门控（``_FACTS_INJECT_WHERE``）是正文注入 / 导出附录 / 预检桥接
    **共用**的唯一口径。若某一处放宽，AI 编造值或过期值会重新进入交付物。
    """
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f-depth", "基坑开挖深度", "16m", is_resolved=1, is_stale=0)
    assert await load_resolved_facts_for_scope(db_conn, sid), "前置条件：未过期事实应可注入"

    await db_conn.execute("UPDATE global_facts SET is_stale=1 WHERE id='f-depth'")
    await db_conn.commit()

    facts = await load_resolved_facts_for_scope(db_conn, sid)
    assert facts == [], f"过期事实必须被注入门控排除，实际仍返回 {facts}"
    assert pe.check_hazard_params(_hazard_ctx(sid, facts)) == [], "过期事实不得参与危大判定"


# ===========================================================================
# CHAIN-3 · 事实过期 → 必须进复核清单（不得静默消失）
# ===========================================================================


async def test_stale_fact_is_visible_in_pending_confirmation_list(db_conn):
    """被门控排除的过期事实必须出现在「事实待确认」清单。

    注入门控是四档（``has_conflict=0 AND is_resolved=1 AND is_simulated=0
    AND is_stale=0``）；被排除的事实若不进清单 ⇒ 底稿写「事实待确认 0 项」，
    用户以为无需复核，而正文/导出实际少一条关键事实。
    """
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f-stale", "基坑开挖深度", "16m", is_resolved=1, is_stale=1)
    await _seed_fact(db_conn, pid, sid, "f-ok", "总工期", "90日历天", is_resolved=1, is_stale=0)

    out = await dossier.collect_facts(db_conn, sid, pid)
    titles = [r["title"] for r in out["items"]]
    assert "基坑开挖深度" in titles, "过期事实必须出现在待确认清单"
    assert "总工期" not in titles, "正常已确认事实不应进入待确认清单"


# ===========================================================================
# CHAIN-4 · 正文规范化 → 落库判据 → 预检/导出字数口径
# ===========================================================================


def test_sanitize_only_rewrite_counts_as_content_changed():
    """仅净化（无子标题改写）的正文也必须被判为「正文变了」。

    ``normalize_section_content_subheadings`` 的 ``changed`` 只统计子标题
    改写；净化型改写（字面 ``\\n`` 还原 / 分页页脚整行删除）会让正文与字数
    一起变，但 ``changed`` 仍为 False ⇒ 以它为门控的落库路径落**旧字数**。
    """
    raw = "一、编制说明\\n本方案依据现行规范编制\n第 3 页 共 5 页"
    normalized, _stats = normalize_raw_content(raw)
    assert normalized != raw, "样例必须触发净化型改写，否则本用例是假绿"
    assert content_actually_changed(raw, normalized, False) is True, "净化型改写必须判为正文已变"
    # 无改写时必须判为未变（不得把判据做成恒真）
    assert content_actually_changed(normalized, normalized, False) is False


def test_low_word_threshold_shared_by_preflight_and_export():
    """低字数阈值在预检与导出两侧必须同源（不得各写一份字面量）。"""
    assert export_router.LOW_WORD_THRESHOLD == pe.LOW_WORD_THRESHOLD
    assert pe.LOW_WORD_THRESHOLD == 100


def test_content_plan_chart_flag_single_source_across_entrypoints():
    """编排数据的两个入口对同一份 JSON 必须给出同一结论。

    ``"chart_needed": "false"`` 在旧 ``from_json`` 里被 ``bool("false")`` 判成
    **真**（Python 语义），而 ``validate`` 判假 ⇒ 同一份编排数据，回读入口与
    产出入口结论相反。
    """
    from app.services import content_plan as cp

    payload = {"writing_focus": "重点写施工组织", "chart_needed": "false", "chart_types": ["not_a_chart"]}

    produced = cp.ContentPlanData.validate(dict(payload))
    reloaded = cp.ContentPlanData.from_json(dict(payload))
    assert produced.chart_needed is False
    assert reloaded.chart_needed is False, '"false" 字符串不得被判成真'
    assert produced.chart_types == [], "非白名单图表类型必须被钳制"
    assert reloaded.chart_types == [], "非白名单图表类型必须被钳制（回读入口同口径）"


# ===========================================================================
# CHAIN-5 · 章节 id 贯通（正文 → 预检 → 前端定位 / 修复）
# ===========================================================================


def test_preflight_completeness_finding_carries_real_section_id():
    """完整性类 finding 必须带真实 ``section_id``。

    否则前端「问题定位跳转」不可达，修复侧只能按标题反查 —— 而「施工准备」
    这类同名章节在各章下是常态 ⇒ 张冠李戴。
    """
    ctx = pe.PreflightContext(
        scheme_id="s1",
        sections=[
            {"id": "sec-emp", "title": "工程概况", "content": "", "word_count": 0, "parent_id": ""},
            {"id": "sec-other", "title": "施工部署", "content": "有正文", "word_count": 50, "parent_id": ""},
        ],
    )
    findings = pe.check_completeness(ctx)
    empty = [f for f in findings if "正文为空" in (f.get("detail") or "")]
    assert empty, f"应产出「正文为空」类发现，实际 findings={findings}"
    assert empty[0].get("section_id") == "sec-emp", empty[0]
    assert empty[0].get("section_title") == "工程概况", empty[0]


def test_audit_rule_namespace_single_source_across_modules():
    """规则基号解析在评分 / 预检 / 注册表三处必须同结论。

    同一处数值冲突若在「注册表 / 评分 / 预检」解析出不同基号 ⇒ 同族被当两族
    ⇒ 重复扣分、用户看到两条几乎相同的问题。
    """
    assert asc._base_rule_key("CON-SCAN-1") == "CON-07"
    assert pe._resolve_rule("CON-SCAN-1").rule_id == "CON-07"
    assert ar._resolve_base_rule("CMP-SC-1").rule_id == "SCP-01"
    assert ar.get_rule("SCP-01") is not None
