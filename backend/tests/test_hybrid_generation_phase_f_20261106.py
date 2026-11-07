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
from app.services import clause_candidates, clause_store, section_router
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

    async def fake_search(db, *, standard_bases=(), query="", limit=10, **kwargs):
        calls.append({"query": query, "limit": limit, "standard_bases": tuple(standard_bases)})
        return _result(_clause())

    res = await section_router.search_for_section(
        db_conn, "验收标准", category_ids=("formwork",), search_fn=fake_search
    )
    assert res.status == "ok" and len(res.hits) == 1
    assert calls == [
        {
            "query": "验收标准",
            "limit": 8,
            "standard_bases": ("GB50204", "GB50666", "GB55023", "JGJ162", "JGJ300"),
        }
    ]


@pytest.mark.asyncio
async def test_empty_corpus_is_no_corpus_reason_not_silent_skip(db_conn):
    """库空（有表、0 条）必须落 ``no_corpus``，不得静默跳过，也不得记成命中。"""
    section_router.reset_clause_search_cache()
    res = await section_router.search_for_section(db_conn, "验收标准", category_ids=("formwork",))
    assert res.status == "no_corpus"
    assert res.reason_code == "no_corpus"
    block = section_router.clause_context_block(res)
    assert "无法判定" in block
    assert "本节未经规范校验" in block


@pytest.mark.asyncio
async def test_missing_table_is_no_corpus_not_error():
    """``standard_clauses`` 表缺失属于 ``no_corpus``（环境事实），不是检索异常。"""
    import aiosqlite

    section_router.reset_clause_search_cache()
    async with aiosqlite.connect(":memory:") as conn:
        res = await section_router.search_for_section(conn, "验收标准", category_ids=("formwork",))
    assert res.status == "no_corpus"
    assert res.reason_code == "no_corpus"
    assert "表缺失" in res.message


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
    # 原因码与「本节未经规范校验」必须由同一处生成并写进报告
    assert "search_report_fields(" in src
    assert "本节未经规范校验" in src
    # 只对 RAG 节点检索（经验驱动不查库，预算不浪费）
    assert '_hroute.strategy == "rag"' in src


def test_startup_warns_when_hybrid_on_but_clause_library_unavailable():
    """开关打开 + 条款库不可用：启动路径必须留 WARNING，不能静默。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "R179 Phase F 混合生成已开启，但条款库不可用" in src
    assert "R179 Phase F 混合生成已开启，但条款库为空" in src
    assert "count_active_clauses" in src


def test_hybrid_flag_defaults_off_and_blacklist_configured():
    assert settings.hybrid_generation_enabled is False
    assert "根据实际情况确定" in settings.hybrid_vague_blacklist
    assert len(section_router.blacklist_from_settings()) > 0


def test_blacklist_from_settings_is_overridable(monkeypatch):
    monkeypatch.setattr(settings, "hybrid_vague_blacklist", "另行研究；专题论证")
    assert section_router.blacklist_from_settings() == ("另行研究", "专题论证")


# ------------------------------------------------ 2026-11-07 检索重做加固


@pytest.fixture(autouse=True)
def _isolate_clause_cache():
    """检索缓存是进程内复用，测试之间必须隔离（否则同 query 会读到上一库结果）。"""
    section_router.reset_clause_search_cache()
    yield
    section_router.reset_clause_search_cache()


def test_clause_candidate_table_matches_csv():
    """代码常量是 CSV 的派生副本：逐行比对，禁止手工分叉。"""
    import csv
    from pathlib import Path

    csv_path = Path(__file__).resolve().parents[2] / "docs" / "条款库_17类核心规范清单_R179_20261106.csv"
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    rows = list(csv.DictReader(lines[1:]))
    actual: dict[str, list[str]] = {}
    for row in rows:
        actual.setdefault(row["template_key"], []).append(row["standard_code"])
    assert set(actual) == set(clause_candidates.CATEGORY_STANDARD_BASES)
    for key, expected in clause_candidates.CATEGORY_STANDARD_BASES.items():
        assert actual[key] == list(expected), key


def test_candidate_resolution_and_unmapped_vocab():
    """文种收敛：命中的类别给出基号；17 类之外不得臆造候选。"""
    formwork = clause_candidates.candidate_standard_bases(("formwork",), "formwork_tall")
    assert "GB55023" in formwork and "JGJ162" in formwork
    foundation = clause_candidates.candidate_standard_bases(("foundation_pit",), "earthwork")
    assert "GB55003" in foundation and "GB50497" in foundation
    # 顶管 / 暗挖不在 17 类清单 → 空集合（由调用方落 no_standard_code）
    assert clause_candidates.candidate_standard_bases((), "pipe_jacking") == ()
    assert clause_candidates.candidate_standard_bases(("other",), "underground_excavation") == ()


# ---- 夹具：51 条条款（45 目标 + 6 干扰项），只存在于测试内，不写生产库 ----

_STANDARD_NAMES = {
    "GB 55023": "施工脚手架通用规范",
    "JGJ 162": "建筑施工模板安全技术规范",
    "GB 50666": "混凝土结构工程施工规范",
    "GB 50204": "混凝土结构工程施工质量验收规范",
    "JGJ 300": "建筑施工临时支撑结构技术规范",
    "GB 55003": "建筑与市政地基基础通用规范",
    "JGJ 120": "建筑基坑支护技术规程",
    "JGJ 311": "建筑深基坑工程施工安全技术规范",
    "GB 50497": "建筑基坑工程监测技术标准",
    "GB 50202": "建筑地基基础工程施工质量验收标准",
    "JGJ 130": "建筑施工扣件式钢管脚手架安全技术规范",
    "JGJ/T 231": "建筑施工承插型盘扣式钢管脚手架安全技术标准",
    "JGJ 202": "建筑施工工具式脚手架安全技术规范",
    "JGJ 80": "建筑施工高处作业安全技术规范",
    "GB 50300": "建筑工程施工质量验收统一标准",
    "GB 55032": "建筑与市政工程施工质量控制通用规范",
    "JGJ 59": "建筑施工安全检查标准",
    "GB 55034": "建筑与市政施工现场安全卫生与职业健康通用规范",
    "GB 50720": "建设工程施工现场消防安全技术规范",
    "JGJ 146": "建设工程施工现场环境与卫生标准",
}

_FIXTURE_ROWS: list[tuple[str, str, str, str, str, str, int]] = [
    # (uid, standard_code, version, clause_no, clause_title, excerpt, mandatory)
    # --- formwork 目标（9）
    (
        "u-fw-1",
        "JGJ 162",
        "2008",
        "6.1.1",
        "模板支撑架搭设",
        "模板支撑架搭设应按专项施工方案执行，立杆间距与步距应符合设计要求。",
        0,
    ),
    (
        "u-fw-2",
        "JGJ 162",
        "2008",
        "6.4.1",
        "支撑架拆除",
        "模板支撑架拆除应经项目技术负责人确认，混凝土强度达到拆模要求后方可拆除。",
        0,
    ),
    (
        "u-fw-3",
        "JGJ 162",
        "2008",
        "6.1.3",
        "立杆间距与步距",
        "立杆间距与步距不得超过方案规定值，立杆接长应采用对接扣件连接。",
        0,
    ),
    (
        "u-fw-4",
        "GB 50204",
        "2015",
        "4.2.1",
        "模板安装允许偏差",
        "模板安装允许偏差应符合规范要求，轴线位置允许偏差 5mm。",
        0,
    ),
    (
        "u-fw-5",
        "GB 50666",
        "2011",
        "7.1.1",
        "混凝土浇筑与振捣",
        "混凝土浇筑与振捣应分层连续进行，振捣棒插入下层混凝土 50mm至100mm。",
        0,
    ),
    (
        "u-fw-6",
        "GB 55023",
        "2022",
        "4.1.1",
        "支撑体系构造要求",
        "模板支撑体系构造应保证整体稳定，水平剪刀撑与竖向剪刀撑应按规范设置。",
        0,
    ),
    (
        "u-fw-7",
        "JGJ 300",
        "2013",
        "5.1.1",
        "承插型盘扣式支架",
        "承插型盘扣式支架搭设应检查盘扣节点连接与可调托撑伸出长度。",
        0,
    ),
    ("u-fw-8", "JGJ 162", "2008", "5.2.1", "荷载计算", "模板支撑架荷载计算应包括模板自重、混凝土自重与施工荷载。", 0),
    (
        "u-fw-9",
        "GB 50204",
        "2015",
        "4.3.1",
        "模板拆除条件",
        "模板拆除条件应根据同条件养护试块强度确定，大跨度梁达到设计强度后方可拆除。",
        0,
    ),
    # --- foundation 目标（9）
    ("u-fp-1", "JGJ 311", "2013", "4.1.2", "基坑开挖分层", "基坑开挖应分层分段进行，每层开挖深度不宜超过 2m。", 0),
    (
        "u-fp-2",
        "GB 50497",
        "2019",
        "3.0.1",
        "基坑支护变形监测",
        "基坑支护变形监测应覆盖支护结构水平位移、周边地表沉降与地下水位。",
        0,
    ),
    (
        "u-fp-3",
        "JGJ 120",
        "2012",
        "8.1.1",
        "降水与排水措施",
        "基坑降水与排水措施应连续运行，降水井停抽应经设计确认。",
        0,
    ),
    (
        "u-fp-4",
        "GB 50202",
        "2018",
        "5.1.1",
        "基坑土方开挖验收",
        "基坑土方开挖验收应检查标高、长度、宽度与边坡坡度。",
        0,
    ),
    (
        "u-fp-5",
        "GB 55003",
        "2021",
        "3.1.1",
        "地基基础施工安全",
        "地基基础施工应查明地下管线与周边环境，边坡失稳风险应提前控制。",
        0,
    ),
    (
        "u-fp-6",
        "JGJ 120",
        "2012",
        "4.1.2",
        "支护结构选型",
        "基坑支护结构选型应根据开挖深度、土层条件与周边环境确定。",
        0,
    ),
    (
        "u-fp-7",
        "GB 50497",
        "2019",
        "4.0.2",
        "监测频率",
        "基坑监测频率应根据开挖阶段与变形速率调整，出现预警时应加密监测。",
        0,
    ),
    ("u-fp-8", "JGJ 311", "2013", "3.1.1", "地下水控制", "地下水控制应结合土层渗透性与降水影响半径设计。", 0),
    ("u-fp-9", "GB 50202", "2018", "6.1.1", "基坑回填", "基坑回填应分层夯实，压实系数应符合设计要求。", 0),
    # --- scaffold 目标（9）
    ("u-sc-1", "JGJ 130", "2011", "6.2.1", "脚手架连墙件设置", "脚手架连墙件应按三步三跨设置，连墙件应靠近主节点。", 0),
    ("u-sc-2", "JGJ 130", "2011", "6.10.1", "悬挑脚手架卸荷", "悬挑脚手架卸荷钢丝绳应张紧，悬挑梁锚固应可靠。", 0),
    ("u-sc-3", "JGJ/T 231", "2021", "5.1.1", "盘扣式脚手架搭设", "盘扣式脚手架搭设应检查立杆垂直度与水平杆步距。", 0),
    (
        "u-sc-4",
        "JGJ 202",
        "2010",
        "4.1.1",
        "附着式升降脚手架",
        "附着式升降脚手架升降前应检查防坠装置与同步控制系统。",
        0,
    ),
    ("u-sc-5", "JGJ 80", "2016", "4.1.2", "高处作业临边防护", "高处作业临边防护栏杆高度不应低于 1.2m。", 0),
    ("u-sc-6", "GB 55023", "2022", "3.2.1", "脚手架基础", "脚手架基础应平整坚实，立杆底部应设置垫板与可调底座。", 0),
    ("u-sc-7", "JGJ 130", "2011", "4.2.1", "脚手架荷载", "脚手架荷载应包括施工荷载、风荷载与构配件自重。", 0),
    ("u-sc-8", "JGJ 202", "2010", "5.1.1", "脚手架验收", "脚手架搭设完成后应分段验收，验收合格后方可使用。", 0),
    ("u-sc-9", "JGJ 80", "2016", "5.1.1", "安全网设置", "高处作业安全网设置应封闭严密，网绳连接应牢固。", 0),
    # --- quality 目标（9）
    (
        "u-qu-1",
        "GB 50300",
        "2013",
        "5.0.1",
        "检验批质量验收",
        "检验批质量验收应按主控项目与一般项目划分，主控项目必须全部合格。",
        0,
    ),
    (
        "u-qu-2",
        "GB 50300",
        "2013",
        "3.0.1",
        "质量验收划分",
        "建筑工程质量验收应划分为单位工程、分部工程、分项工程与检验批。",
        0,
    ),
    (
        "u-qu-3",
        "GB 55032",
        "2022",
        "4.1.1",
        "混凝土结构验收",
        "混凝土结构验收应检查强度、钢筋保护层厚度与外观质量。",
        0,
    ),
    (
        "u-qu-4",
        "GB 50300",
        "2013",
        "6.0.1",
        "分部工程验收",
        "分部工程验收应由总监理工程师组织，地基与基础分部应验收合格。",
        0,
    ),
    (
        "u-qu-5",
        "GB 50204",
        "2015",
        "8.1.1",
        "现浇结构外观质量",
        "现浇结构外观质量不应有严重缺陷，严重缺陷应经技术处理。",
        0,
    ),
    (
        "u-qu-6",
        "GB 50204",
        "2015",
        "9.1.1",
        "结构实体检验",
        "结构实体检验应包括混凝土强度、钢筋保护层厚度与结构位置尺寸。",
        0,
    ),
    ("u-qu-7", "GB 55032", "2022", "2.1.1", "施工质量通用要求", "建筑工程施工质量应满足设计文件与验收规范要求。", 0),
    ("u-qu-8", "GB 50300", "2013", "4.0.1", "检验批抽样方案", "检验批抽样方案应根据生产控制水平与检验项目确定。", 0),
    (
        "u-qu-9",
        "GB 55032",
        "2022",
        "3.0.1",
        "质量验收程序",
        "施工质量验收应按检验批、分项、分部与单位工程逐级进行。",
        0,
    ),
    # --- safety_management 目标（9）
    (
        "u-sf-1",
        "JGJ 59",
        "2011",
        "3.2.1",
        "安全检查评分",
        "安全检查评分应按安全管理、文明施工、脚手架与高处作业等分项打分。",
        0,
    ),
    (
        "u-sf-2",
        "GB 50720",
        "2011",
        "3.1.1",
        "现场临时消防设施",
        "施工现场临时消防设施应与在建工程同步设置，消防水源应充足。",
        0,
    ),
    (
        "u-sf-3",
        "GB 55034",
        "2022",
        "3.1.1",
        "施工现场安全防护",
        "施工现场安全防护应设置安全通道、防护棚与临边防护。",
        0,
    ),
    ("u-sf-4", "JGJ 146", "2013", "4.1.1", "文明施工管理", "文明施工管理应包括围挡、道路硬化、材料堆放与噪声控制。", 0),
    (
        "u-sf-5",
        "JGJ 59",
        "2011",
        "4.1.1",
        "安全管理检查",
        "安全管理检查应核查安全生产责任制、专项方案与安全技术交底。",
        0,
    ),
    ("u-sf-6", "GB 50720", "2011", "4.2.1", "消防通道", "施工现场消防通道应保持畅通，宽度不应小于 4m。", 0),
    (
        "u-sf-7",
        "GB 55034",
        "2022",
        "5.1.1",
        "危险性较大工程监控",
        "危险性较大的分部分项工程应编制专项方案并实施旁站监控。",
        0,
    ),
    (
        "u-sf-8",
        "JGJ 146",
        "2013",
        "3.1.1",
        "现场围挡",
        "施工现场围挡应连续设置，市区主要路段围挡高度不应低于 2.5m。",
        0,
    ),
    ("u-sf-9", "JGJ 59", "2011", "5.1.1", "施工用电检查", "施工用电检查应核查接地接零、漏电保护与配电箱设置。", 0),
    # --- 干扰项（6）：跨类别同词，mandatory=1 + 早条文号，全库匹配时会抢到第 1 位
    (
        "d-1",
        "GB 50300",
        "2013",
        "1.0.1",
        "模板支撑架搭设",
        "模板支撑架搭设应按专项施工方案执行，立杆间距与步距应符合设计要求。",
        1,
    ),
    ("d-2", "JGJ 162", "2008", "1.0.1", "基坑开挖分层", "基坑开挖应分层分段进行，每层开挖深度不宜超过 2m。", 1),
    ("d-3", "JGJ 59", "2011", "1.0.1", "脚手架连墙件设置", "脚手架连墙件应按三步三跨设置，连墙件应靠近主节点。", 1),
    (
        "d-4",
        "JGJ 120",
        "2012",
        "1.0.1",
        "检验批质量验收",
        "检验批质量验收应按主控项目与一般项目划分，主控项目必须全部合格。",
        1,
    ),
    (
        "d-5",
        "GB 55032",
        "2022",
        "1.0.1",
        "安全检查评分",
        "安全检查评分应按安全管理、文明施工、脚手架与高处作业等分项打分。",
        1,
    ),
    (
        "d-6",
        "GB 50666",
        "2011",
        "1.0.1",
        "基坑支护变形监测",
        "基坑支护变形监测应覆盖支护结构水平位移、周边地表沉降与地下水位。",
        1,
    ),
]

#: 查询 → (文种上下文, 期望命中的 clause_uid)。
_EVAL_QUERIES: list[tuple[str, dict, str]] = [
    ("模板支撑架搭设", {"category_ids": ("formwork",), "template_key": "formwork_tall"}, "u-fw-1"),
    ("支撑架拆除", {"category_ids": ("formwork",), "template_key": "formwork"}, "u-fw-2"),
    ("立杆间距与步距", {"category_ids": ("formwork",), "template_key": "formwork"}, "u-fw-3"),
    ("模板安装允许偏差", {"category_ids": ("formwork",), "template_key": "formwork"}, "u-fw-4"),
    ("混凝土浇筑与振捣", {"category_ids": ("formwork",), "template_key": "formwork"}, "u-fw-5"),
    ("基坑开挖分层", {"category_ids": ("foundation_pit",), "template_key": "earthwork"}, "u-fp-1"),
    ("基坑支护变形监测", {"category_ids": ("foundation_pit",), "template_key": "monitoring"}, "u-fp-2"),
    ("降水与排水措施", {"category_ids": ("foundation_pit",), "template_key": "foundation_pit"}, "u-fp-3"),
    ("脚手架连墙件设置", {"category_ids": ("scaffold",), "template_key": "scaffold_cantilever"}, "u-sc-1"),
    ("悬挑脚手架卸荷", {"category_ids": ("scaffold",), "template_key": "scaffold_cantilever"}, "u-sc-2"),
    ("高处作业临边防护", {"category_ids": ("scaffold",), "template_key": "scaffold_ground"}, "u-sc-5"),
    ("检验批质量验收", {"category_ids": (), "template_key": "quality"}, "u-qu-1"),
    ("安全检查评分", {"category_ids": (), "template_key": "safety_management"}, "u-sf-1"),
    ("现场临时消防设施", {"category_ids": (), "template_key": "safety_management"}, "u-sf-2"),
]


async def _seed_fixture(db) -> None:
    rows = [
        {
            "clause_uid": uid,
            "standard_code": code,
            "standard_name": _STANDARD_NAMES.get(code, code),
            "version": version,
            "clause_no": clause_no,
            "clause_title": title,
            "excerpt": excerpt,
            "excerpt_type": "key_sentence",
            "mandatory": mandatory,
            "applicability": "通用",
            "license_note": "仅摘录要点，禁止全文转载",
            "source_url": f"https://example.com/{code.replace(' ', '')}/{clause_no}",
            "page_ref": "",
            "source_kind": "public",
            "verification_status": "unverified",
            "status": "active",
        }
        for uid, code, version, clause_no, title, excerpt, mandatory in _FIXTURE_ROWS
    ]
    await clause_store.import_clauses(db, rows, confirm_license=True, batch_id="fixture-phase-f")


def _hit_metrics(results) -> tuple[float, float]:
    count = len(results)
    hit1 = sum(1 for hits, uid in results if hits and hits[0].clause_uid == uid) / count
    recall5 = sum(1 for hits, uid in results if any(h.clause_uid == uid for h in hits[:5])) / count
    return hit1, recall5


@pytest.mark.asyncio
async def test_scoped_retrieval_hit1_and_recall5(db_conn):
    """改后基线：≥40 条夹具、≥10 查询，候选内检索的 Hit@1 / 召回@5。"""
    await _seed_fixture(db_conn)
    assert len(_FIXTURE_ROWS) >= 40 and len(_EVAL_QUERIES) >= 10

    results = []
    for title, context, expected_uid in _EVAL_QUERIES:
        res = await section_router.search_for_section(db_conn, title, **context)
        assert res.status == "ok", (title, res.status, res.message)
        results.append((res.hits, expected_uid))
    hit1, recall5 = _hit_metrics(results)
    assert hit1 >= 0.9, f"scoped Hit@1={hit1:.3f}"
    assert recall5 >= 0.95, f"scoped Recall@5={recall5:.3f}"


@pytest.mark.asyncio
async def test_unscoped_mutation_lowers_hit1(db_conn):
    """变异验证（红）：去掉标准号候选约束、退回全库匹配，Hit@1 必须下降。

    若生产实现被改成「忽略候选集」，``test_scoped_retrieval_hit1_and_recall5``
    会直接变红；本用例同时给出两条路径的量化对照。
    """
    await _seed_fixture(db_conn)
    scoped = []
    unscoped = []
    for title, context, expected_uid in _EVAL_QUERIES:
        res = await section_router.search_for_section(db_conn, title, **context)
        scoped.append((res.hits, expected_uid))
        loose = await clause_store.search_clauses_in_candidates(db_conn, standard_bases=None, query=title, limit=8)
        unscoped.append((loose.hits, expected_uid))
    scoped_hit1, _ = _hit_metrics(scoped)
    loose_hit1, _ = _hit_metrics(unscoped)
    assert scoped_hit1 > loose_hit1, f"scoped={scoped_hit1:.3f} unscoped={loose_hit1:.3f}"


@pytest.mark.asyncio
async def test_reason_codes_are_four_distinct_not_one_undetermined(db_conn):
    """变异验证（红）：把四种原因码揉成一个 ``undetermined`` 会让本用例失败。"""
    import aiosqlite

    await _seed_fixture(db_conn)

    no_standard = await section_router.search_for_section(
        db_conn, "验收标准", category_ids=(), template_key="pipe_jacking"
    )
    no_match = await section_router.search_for_section(db_conn, "完全不存在的量子纠缠词组", category_ids=("formwork",))
    async with aiosqlite.connect(":memory:") as bare:
        no_corpus = await section_router.search_for_section(bare, "验收标准", category_ids=("formwork",))

    async def boom(db, **kwargs):
        raise RuntimeError("boom")

    error = await section_router.search_for_section(db_conn, "验收标准", category_ids=("formwork",), search_fn=boom)

    assert no_standard.status == "no_standard_code"
    assert no_match.status == "no_match"
    assert no_corpus.status == "no_corpus"
    assert error.status == "error"
    assert {
        no_standard.status,
        no_match.status,
        no_corpus.status,
        error.status,
    } == {"no_standard_code", "no_match", "no_corpus", "error"}

    for result in (no_standard, no_match, no_corpus, error):
        fields = section_router.search_report_fields(result)
        assert fields["search_reason_code"] == result.status
        assert fields["section_unverified"] is True
        assert fields["section_unverified_note"].startswith("本节未经规范校验")
        assert fields["skipped_checks"]


@pytest.mark.asyncio
async def test_budget_exceeded_is_reported_not_silently_dropped(db_conn):
    budget = section_router.ClauseSearchBudget(limit=2)
    calls: list[int] = []

    async def fake_search(db, **kwargs):
        calls.append(1)
        return _result(_clause())

    for index in range(3):
        res = await section_router.search_for_section(
            db_conn,
            f"预算测试查询{index}",
            category_ids=("formwork",),
            search_fn=fake_search,
            budget=budget,
        )
    assert len(calls) == 2
    assert res.status == "error" and res.budget_exceeded
    assert "预算超限" in res.message
    assert budget.as_dict()["skipped"] == 1
    fields = section_router.search_report_fields(res, budget=budget)
    assert fields["budget"]["exhausted"] is True


@pytest.mark.asyncio
async def test_same_candidates_and_query_reuses_cached_result(db_conn):
    calls: list[dict] = []

    async def fake_search(db, **kwargs):
        calls.append(kwargs)
        return _result(_clause())

    for _ in range(2):
        await section_router.search_for_section(db_conn, "验收标准", category_ids=("formwork",), search_fn=fake_search)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_result_cache_has_upper_bound(db_conn):
    async def empty_search(db, **kwargs):
        return clause_store.ClauseSearchResult(status="no_match", reason_code="no_match")

    for index in range(300):
        await section_router.search_for_section(
            db_conn,
            f"空结果缓存{index}",
            category_ids=("formwork",),
            search_fn=empty_search,
        )
    assert section_router.clause_search_cache_size() <= 256


@pytest.mark.asyncio
async def test_search_timeout_is_error_reason(db_conn):
    import asyncio

    async def slow_search(db, **kwargs):
        await asyncio.sleep(0.05)
        return _result(_clause())

    res = await section_router.search_for_section(
        db_conn, "验收标准", category_ids=("formwork",), search_fn=slow_search, timeout=0.001
    )
    assert res.status == "error"
    assert "超时" in res.message
