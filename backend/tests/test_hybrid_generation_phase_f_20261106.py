"""R179 Phase F · 分区混合生成 + 数值锁（2026-11-06）。

验收线
------
1. 五类 ``content_type`` 100% 分流：规范驱动（rag）/ 经验驱动（llm）；
2. 数值锁机械可验：条款数值 ∪ 项目事实值之外的数值一律拦截；
   变异：把允许值 5m 改成 6m，对应用例必须变红（本文件用显式 5m→6m 用例咬住）；
3. 经验段两条硬约束：必须引用项目事实值；黑名单表述命中即计缺陷（可配置）；
4. 事实缺失必须显式标注「待确认」，不得编造、不得悄悄留空；
5. 开关默认关闭；检索与 LLM 均可注入，用例不触达任何外部服务。

本阶段 F 不新增 LLM 调用：条款上下文与数值锁清单只追加进既有可注入生成通道
（``chat_with_fallback``），因此本文件全部为纯函数 / 注入假检索实现，零网络。
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.services import clause_store, section_router
from app.services.numeric_lock import (
    build_allowed_numbers,
    check_text,
    enforce_text,
    extract_numbers,
)
from app.services.outline_templates import CONTENT_TYPES


def _clause(**overrides) -> clause_store.ClauseRef:
    row = {
        "clause_uid": "c-5-2-1",
        "standard_code": "GB 50007-2011",
        "standard_name": "建筑地基基础设计规范",
        "version": "2011",
        "clause_no": "第5.2.1条",
        "excerpt": "基础底面压力应符合设计要求。",
        "applicability": "天然地基",
        "verification_status": "unverified",
        "license_note": "仅摘录要点，禁止全文转载",
        "source_url": "https://example.com/gb50007/5.2.1",
        "page_ref": "",
        "source_kind": "public",
        "match_type": "exact",
    }
    row.update(overrides)
    return clause_store.ClauseRef(**row)


def _result(*hits, status: str = "ok", message: str = "") -> clause_store.ClauseSearchResult:
    return clause_store.ClauseSearchResult(status=status, hits=list(hits), message=message)


# ---------------------------------------------------------------- 分流


@pytest.mark.parametrize("content_type", CONTENT_TYPES)
def test_content_type_routing_is_total(content_type):
    """五类枚举必须全部映射到且仅映射到一个策略。"""
    strategy = section_router.strategy_for(content_type)
    assert strategy in {"rag", "llm"}
    assert strategy == ("rag" if content_type in ("standard", "calculation", "verification") else "llm")


def test_route_reads_outline_json_meta():
    """Phase B 写入 sections.outline_json 的元数据必须被路由直接消费。"""
    leaf = {
        "title": "安全保证措施",
        "outline_json": (
            '{"id": "3", "level": 2, "content_type": "standard", "fact_status": "partial",'
            ' "fact_values": {"support_height": {"value": "支模高度 8.6m"}},'
            ' "missing_fact_deps": ["span"]}'
        ),
    }
    route = section_router.route_node(leaf)
    assert route.content_type == "standard"
    assert route.strategy == "rag"
    assert route.meta_source == "outline_json"
    assert route.fact_values == {"support_height": "支模高度 8.6m"}
    assert route.missing_deps == ("span",)


def test_route_falls_back_to_title_when_meta_missing():
    route = section_router.route_node({"title": "施工部署"})
    assert route.meta_source == "title"
    assert route.content_type in CONTENT_TYPES
    assert route.strategy in {"rag", "llm"}


# ---------------------------------------------------------------- 数值锁


def test_numeric_lock_allows_clause_and_fact_values_only():
    allowed = build_allowed_numbers(
        ["搭设高度不应超过 5m，立杆间距 1.2m。"],
        ["开挖深度 3.2m，周边建筑距离 8.5m"],
    )
    ok_text = "脚手架搭设高度 5m，立杆间距 1.2m；基坑开挖深度 3.2m。"
    assert check_text(ok_text, allowed).ok


def test_numeric_lock_rejects_extra_value_mutation_5m_to_6m():
    """变异锚点：允许集合里只有 5m；正文写 6m 必红。"""
    allowed = build_allowed_numbers(["搭设高度不应超过 5m。"], ["开挖深度 3.2m"])
    bad = "脚手架搭设高度 6m，满足规范要求。"
    result = check_text(bad, allowed)
    assert not result.ok
    assert [v["raw"] for v in result.violations] == ["6m"]


def test_numeric_lock_unit_equivalence():
    """5m 与 5000mm 是同一数值（长度族换算），不得误判为编造。"""
    allowed = build_allowed_numbers(["搭设高度不应超过 5m。"])
    assert check_text("搭设高度 5000mm。", allowed).ok


def test_numeric_lock_binds_sentence_to_single_clause():
    """A=5m、B=8m 同批返回时，把引用 A 的句子改成 8m 必须红（不跨条款合并）。"""
    hit_a = _clause(clause_uid="uid-a", clause_no="第5.2.1条", excerpt="搭设高度不应超过 5m。")
    hit_b = _clause(clause_uid="uid-b", clause_no="第6.1.1条", excerpt="搭设高度不应超过 8m。")
    route = section_router.SectionRoute(
        title="安全保证措施", content_type="standard", strategy="rag", fact_status="resolved"
    )
    scoped = section_router.allowed_numbers_for(route, [hit_a, hit_b])

    ok_text = "依据第5.2.1条，搭设高度 5m。依据第6.1.1条，搭设高度 8m。"
    assert check_text(ok_text, scoped).ok

    bad = "依据第5.2.1条，搭设高度 8m。"
    result = check_text(bad, scoped)
    assert not result.ok
    assert "8m" in [item["raw"] for item in result.violations]


def test_numeric_lock_rejects_cross_clause_stitching_in_one_sentence():
    """同一句里拼接两条条款的数值（5m 来自 A、1.2m 来自 B）同样必须红。"""
    hit_a = _clause(clause_uid="uid-a", clause_no="第5.2.1条", excerpt="搭设高度不应超过 5m。")
    hit_b = _clause(clause_uid="uid-b", clause_no="第6.1.1条", excerpt="立杆间距不得大于 1.2m。")
    route = section_router.SectionRoute(
        title="安全保证措施", content_type="standard", strategy="rag", fact_status="resolved"
    )
    scoped = section_router.allowed_numbers_for(route, [hit_a, hit_b])
    assert not check_text("依据第5.2.1条，搭设高度 5m，立杆间距 1.2m。", scoped).ok
    # 拆成两句、各自绑定本条款后合法。
    assert check_text("依据第5.2.1条，搭设高度 5m。依据第6.1.1条，立杆间距 1.2m。", scoped).ok
    # 第二句不写条款号 → 绑定不到任何条款，判 undetermined（不是通过）。
    ambiguous = check_text("依据第5.2.1条，搭设高度 5m。立杆间距 1.2m。", scoped)
    assert ambiguous.status == "undetermined"
    assert [item["raw"] for item in ambiguous.undetermined] == ["1.2m"]


def test_numeric_lock_without_clause_citation_is_undetermined_not_pass():
    """不写条款号不得绕过锁：检索返回 A(5m)/B(8m)，无条款号的 8m 句子必须 undetermined。"""
    hit_a = _clause(clause_uid="uid-a", clause_no="第5.2.1条", excerpt="搭设高度不应超过 5m。")
    hit_b = _clause(clause_uid="uid-b", clause_no="第6.1.1条", excerpt="搭设高度不应超过 8m。")
    route = section_router.SectionRoute(
        title="安全保证措施", content_type="standard", strategy="rag", fact_status="resolved"
    )
    scoped = section_router.allowed_numbers_for(route, [hit_a, hit_b])

    result = check_text("搭设高度 8m。", scoped)
    assert result.status == "undetermined"
    assert result.violations == []
    assert [item["raw"] for item in result.undetermined] == ["8m"]
    # 无法判定的句子不改写正文（既不通过、也不删句）。
    text, enforced = enforce_text("搭设高度 8m。\n", scoped)
    assert text == "搭设高度 8m。\n"
    assert enforced.status == "undetermined"


def test_numeric_lock_unbound_sentence_fitting_facts_is_ok():
    """未写条款号但数值全部来自项目事实值时，绑定到「项目事实」组即合法。"""
    hit_a = _clause(clause_uid="uid-a", clause_no="第5.2.1条", excerpt="搭设高度不应超过 5m。")
    route = section_router.SectionRoute(
        title="安全保证措施",
        content_type="standard",
        strategy="rag",
        fact_status="resolved",
        fact_values={"excavation_depth": {"value": "3.2m"}},
    )
    scoped = section_router.allowed_numbers_for(route, [hit_a])
    assert check_text("基坑开挖深度 3.2m。", scoped).ok


def test_scoped_enforce_drops_only_cross_clause_sentence():
    hit_a = _clause(clause_uid="uid-a", clause_no="第5.2.1条", excerpt="搭设高度不应超过 5m。")
    hit_b = _clause(clause_uid="uid-b", clause_no="第6.1.1条", excerpt="搭设高度不应超过 8m。")
    route = section_router.SectionRoute(
        title="安全保证措施", content_type="standard", strategy="rag", fact_status="resolved"
    )
    scoped = section_router.allowed_numbers_for(route, [hit_a, hit_b])
    text = "依据第5.2.1条，搭设高度 5m。依据第5.2.1条，搭设高度 8m。"
    new_text, result = enforce_text(text, scoped)
    assert "8m" not in new_text
    assert "依据第5.2.1条，搭设高度 5m" in new_text
    assert result.removed_segments == 1


def test_extract_ignores_numbering_codes_and_dates():
    """章节编号、条款号、标准号、日期不是「正文数值」。"""
    text = (
        "### 5.2.1 施工准备\n"
        "依据 GB 50268-2008 第5.2.1条的规定，2026年11月6日开工，搭设高度 5m。\n"
        "| 表 3-1 参数表 | 5m |\n"
    )
    raws = [token.raw for token in extract_numbers(text)]
    assert raws.count("5m") == 2
    assert "5.2.1" not in raws
    assert "50268" not in raws
    assert "2008" not in raws


def test_enforce_removes_only_violating_sentence():
    allowed = build_allowed_numbers(["搭设高度不应超过 5m。"])
    text = "搭设高度 5m。剪刀撑设置 6m 一道。立杆间距按方案执行。"
    new_text, result = enforce_text(text, allowed)
    assert "6m" not in new_text
    assert "搭设高度 5m" in new_text
    assert "立杆间距按方案执行" in new_text
    assert result.removed_segments == 1
    assert result.blocked_chars > 0


def test_enforce_keeps_compliant_text_byte_identical():
    allowed = build_allowed_numbers(["搭设高度不应超过 5m。"])
    text = "搭设高度 5m。\n\n立杆间距按方案执行。"
    new_text, result = enforce_text(text, allowed)
    assert new_text == text
    assert result.ok


# ---------------------------------------------------------------- 条款上下文


def test_clause_context_contains_excerpt_source_and_match_type():
    block = section_router.clause_context_block(_result(_clause()))
    assert "第5.2.1条" in block
    assert "exact" in block
    assert "https://example.com/gb50007/5.2.1" in block
    assert "c-5-2-1" in block


def test_clause_context_no_corpus_is_undetermined_not_ok():
    block = section_router.clause_context_block(_result(status="no_corpus", message="无法判定：条款库未收录"))
    assert "无法判定" in block
    assert "不得据此编造" in block


def test_rag_hybrid_block_contains_numeric_lock_list():
    route = section_router.route_node({"title": "验收标准"})
    assert route.strategy == "rag"
    hits = [_clause(excerpt="允许偏差不应大于 5mm。")]
    allowed = section_router.allowed_numbers_for(route, hits)
    block = section_router.build_hybrid_block(route, _result(*hits), allowed)
    assert "数值锁" in block and "5mm" in block
    assert "允许偏差不应大于 5mm" in block


# ---------------------------------------------------------------- 经验段约束


def test_experience_requires_fact_citation():
    route = section_router.SectionRoute(
        title="施工部署", content_type="experience", strategy="llm", fact_status="resolved"
    )
    defects = section_router.verify_experience_text("本工程按计划组织施工，落实岗位职责。", route)
    assert any(d["code"] == "no_fact_cited" for d in defects)


def test_experience_cites_fact_passes():
    route = section_router.SectionRoute(
        title="施工部署",
        content_type="experience",
        strategy="llm",
        fact_status="resolved",
        fact_values={"excavation_depth": "基坑开挖深度 3.2m"},
    )
    defects = section_router.verify_experience_text("基坑开挖深度 3.2m，分两层开挖。", route)
    assert defects == []


def test_experience_vague_phrase_is_defect_and_blacklist_configurable():
    route = section_router.SectionRoute(
        title="资源配置",
        content_type="experience",
        strategy="llm",
        fact_status="resolved",
        fact_values={"personnel": "架子工 12 人"},
    )
    text = "架子工 12 人，具体数量根据实际情况确定。"
    codes = [d["code"] for d in section_router.verify_experience_text(text, route)]
    assert "vague_phrase" in codes
    # 黑名单可配置：换一份自定义黑名单后，原句不再命中。
    custom = ("另行研究",)
    assert section_router.verify_experience_text(text, route, blacklist=custom) == []


# ---------------------------------------------------------------- 事实缺失


def test_missing_fact_must_be_explicitly_marked():
    route = section_router.SectionRoute(
        title="工程地质条件",
        content_type="standard",
        strategy="rag",
        fact_status="missing",
        missing_deps=("ground_conditions",),
    )
    block = section_router.build_hybrid_block(route, _result(status="no_corpus"), build_allowed_numbers())
    assert "待确认" in block and "ground_conditions" in block

    exp_route = section_router.SectionRoute(
        title="工程地质条件",
        content_type="experience",
        strategy="llm",
        fact_status="missing",
        missing_deps=("ground_conditions",),
    )
    defects = section_router.verify_experience_text("土层为粉质黏土。", exp_route)
    assert any(d["code"] == "missing_fact_unmarked" for d in defects)
    ok_defects = section_router.verify_experience_text("待确认（缺少 ground_conditions）。", exp_route)
    assert not any(d["code"] == "missing_fact_unmarked" for d in ok_defects)


# ---------------------------------------------------------------- 注入与开关


@pytest.mark.asyncio
async def test_search_is_injectable_and_never_touches_real_service(db_conn):
    """检索函数可注入假实现：F 不新写检索、不依赖外部服务。"""
    calls: list[dict] = []

    async def fake_search(db, *, query="", limit=10, **kwargs):
        calls.append({"query": query, "limit": limit})
        return _result(_clause())

    res = await section_router.search_for_section(db_conn, "验收标准", search_fn=fake_search)
    assert res.status == "ok" and len(res.hits) == 1
    assert calls == [{"query": "验收标准", "limit": 8}]


@pytest.mark.asyncio
async def test_clause_library_unavailable_is_explicit_error_not_silent_skip(db_conn):
    """条款库不可用（如 ``standard_clauses`` 不存在）必须显式报错，不得静默跳过。"""
    import sqlite3

    async def broken_search(db, *, query="", limit=10, **kwargs):
        raise sqlite3.OperationalError("no such table: standard_clauses")

    res = await section_router.search_for_section(db_conn, "验收标准", search_fn=broken_search)
    assert res.status == "error"
    assert "standard_clauses" in res.message
    block = section_router.clause_context_block(res)
    assert "无法判定" in block
    assert "standard_clauses" in block


def test_clause_library_unavailable_must_show_undetermined_in_report():
    """接线锁：条款库不可用时报告必须出现 undetermined，绝不能「表不存在就跳过、啥都没有」。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
    # 注入失败 → 显式登记 error 状态（而不是只打日志）
    assert "混合生成注入失败（记为无法判定）" in src
    assert '"search_status": "error"' in src
    # 后置校验：检索非 ok / 允许集合缺失 → undetermined，并写进 hybrid_report
    assert '_hstate["search_status"] != "ok" or _hallowed is None' in src
    assert '"numeric_lock_status": _hstatus' in src
    # 后置校验自身异常时同样必须留下 undetermined 记录
    assert '"numeric_lock_status": "undetermined" if _hroute.strategy == "rag" else None' in src


def test_hybrid_flag_defaults_off_and_blacklist_configured():
    assert settings.hybrid_generation_enabled is False
    assert "根据实际情况确定" in settings.hybrid_vague_blacklist
    assert len(section_router.blacklist_from_settings()) > 0


def test_blacklist_from_settings_is_overridable(monkeypatch):
    monkeypatch.setattr(settings, "hybrid_vague_blacklist", "另行研究；专题论证")
    assert section_router.blacklist_from_settings() == ("另行研究", "专题论证")
