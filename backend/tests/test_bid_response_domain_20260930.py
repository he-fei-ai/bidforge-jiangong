"""解析提取模块引入招标响应域 + 分段策略 + 断点续跑 的回归护栏（2026-09-30）。

本轮引入 OpenBidKit 易标（参考软件）的「解析提取模块」能力，全部**默认向后兼容**。
本文件锁定以下不变量，防止后续改动造成回退：

1. 域注册表：scheme 域 18 项 / 17 必选 / 13 分组**逐字不变**；招标响应域 18 项 /
   6 必选 / 11 分组；两域 item_id **零交集**（否则主键与下游映射会串）。
2. 主键格式：scheme 域必须是旧格式 ``{project_id}_{item_id}``（历史数据与既有
   WHERE id=? 查询全部继续可用）；bid_response 域为
   ``{project_id}__{domain}__{item_id}``。域名空串/None 必须 fail-closed 到
   scheme，绝不静默落成新格式。
3. 主键唯一出口：`_update_item_status` 必须经 ``build_item_pk``，禁止再出现
   裸 ``f"{project_id}_{item_id}"``（AST 静态扫描）。
4. 提取内容标准单一出口：``build_task_prompt`` 是唯一附加「缺失标注规范」的地方，
   Markdown 项只能追加一次整体无结果规则；「整项无内容」与「局部缺失」两种
   语义必须严格区分（MARKDOWN_MISSING_RESULT vs PARTIAL_MISSING_TEXT）。
5. 技术评分项专判：``is_missing_technical_score_items`` 四向语义
   （小节缺失 / 小节有内容 / 整项缺失 / 无小节）。
6. 分段策略：默认参数下与旧滑动窗口**逐字一致**（A/B 可比对）；均分模式
   不得丢内容、不得尾段塌缩、不得切断代码围栏；硬切点做 Unicode 代理对保护。
7. 断点续跑：默认关闭；开启后跳过已成功项，但 **force_rerun / mode=item 恒忽略**；
   查库异常必须 fail-open（全量执行），绝不因查库失败静默跳过重跑；域间隔离。
8. 路由契约：``GET /items`` 默认返回结构与旧版一致（加法式新增 domain 键）；
   未知域返回空清单 + domain_unknown（fail-closed，不静默回退）；
   招标响应域未启用时 404，而非返回一套无法执行的清单。

测试直接调用路由函数（与既有 test_bid_analysis.py 风格一致），
避免 TestClient 跨事件循环持有 aiosqlite 连接导致的偶发失败。

参考软件对照（只读，不作为测试依赖）：
- client/electron/services/bidAnalysisTask.cjs      ：18 项分类、mode 归一、
  断点续跑（status!=='success' 过滤）、force_rerun 级联清空、日志保留 80 条、
  预热项先行 + 5000ms
- client/electron/utils/userTextSplitter.cjs        ：400000×0.8 均分、5 组边界、
  strict 0.12 / relaxed 0.25 / min 0.35 / max 1.1、围栏与代理对保护
- client/electron/utils/segmentedAiResultMerger.cjs ：分段结果 AI 合并
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import uuid
from pathlib import Path

import pytest

import app.routers.bid_analysis as ba
from app.services import bid_analysis_service as svc
from app.services.bid_analysis_service import (
    ANALYSIS_ITEMS, BID_RESPONSE_ITEMS, REQUIRED_ITEM_IDS,
    MARKDOWN_MISSING_RESULT, PARTIAL_MISSING_TEXT,
    TECH_SCORE_ITEMS_HEADING, EXTRACTION_DOMAINS,
    get_item_def, get_item_domain, get_items_by_domain, get_groups_by_domain,
    get_item_fields, build_json_template, build_task_prompt,
    is_missing_result, is_missing_technical_score_items,
    build_item_pk, parse_item_pk, split_for_analysis, AnalysisConfig,
    fetch_success_item_ids,
)

_BA_SRC = Path(ba.__file__).resolve()
_SVC_SRC = Path(svc.__file__).resolve()


# =========================================================================
# 一、域注册表与分类体系
# =========================================================================
class TestDomainRegistry:
    def test_scheme_domain_items_unchanged(self):
        """scheme 域：18 项历史基线 + 1 项第十六轮新增 = 19 项；必选仍 17 项。

        ✅ 2026-09-30 第十六轮：新增 ``techScoring``（技术评分要求，对齐易标
        《标书智能体（一）》§1.4）。它是 ``required=0``，**不改变必选口径**；
        既有 18 项的 item_id / sort_order 逐字不变（由
        ``test_reference_alignment_20260930::test_existing_18_items_untouched``
        逐项锁定）。
        """
        assert len(get_items_by_domain("scheme")) == 19
        assert get_items_by_domain("scheme") is ANALYSIS_ITEMS
        assert len(REQUIRED_ITEM_IDS) == 17, "新增项为可选，必选口径不得变"

    def test_scheme_domain_groups_count(self):
        """13 个历史分组 + 1 个 scoring 分组 = 14。"""
        assert len(get_groups_by_domain("scheme")) == 14

    def test_bid_response_domain_items(self):
        items = get_items_by_domain("bid_response")
        assert len(items) == 18
        assert items is BID_RESPONSE_ITEMS
        assert len([it for it in items if it["required"]]) == 6

    def test_bid_response_domain_groups_count(self):
        assert len(get_groups_by_domain("bid_response")) == 11

    def test_domains_have_disjoint_item_ids(self):
        """两域 item_id 零交集 —— 否则主键与下游字段映射会互相串。"""
        a = {it["item_id"] for it in get_items_by_domain("scheme")}
        b = {it["item_id"] for it in get_items_by_domain("bid_response")}
        assert not (a & b), f"域交集非空：{sorted(a & b)}"

    def test_bid_response_required_ids_are_the_six_expected(self):
        """易标 6 个必选项（技术评分项属技术方案编制的主要依据）。"""
        expected = {
            "projectOverview", "techRequirements", "projectInfo",
            "partAInfo", "deliveryAndServiceRequirements",
            "responseFileRequirements",
        }
        got = {it["item_id"] for it in get_items_by_domain("bid_response")
               if it["required"]}
        assert got == expected

    def test_groups_cover_every_item(self):
        """两域的分组定义必须覆盖全部解析项（无遗漏、无重复归属）。"""
        for domain in EXTRACTION_DOMAINS:
            items = get_items_by_domain(domain)
            groups = get_groups_by_domain(domain)
            grouped = [it["item_id"] for g in groups for it in g["items"]]
            assert len(grouped) == len(set(grouped)), f"{domain}: 分组重复归属"
            assert set(grouped) == {it["item_id"] for it in items}, (
                f"{domain}: 分组未覆盖全部项")

    def test_get_item_domain_is_single_source(self):
        for domain in EXTRACTION_DOMAINS:
            for it in get_items_by_domain(domain):
                assert get_item_domain(it["item_id"]) == domain

    def test_unknown_item_id_fails_closed(self):
        """未知 item_id 必须返回空串，不得默认当成 scheme 域。"""
        assert get_item_domain("no_such_item") == ""
        assert get_item_domain("") == ""

    def test_item_map_identity_preserved(self):
        """scheme 域定义必须仍是 ANALYSIS_ITEMS 里的同一个 dict（既有调用点零改动）。"""
        for it in ANALYSIS_ITEMS:
            assert get_item_def(it["item_id"]) is it


# =========================================================================
# 二、主键格式与唯一出口
# =========================================================================
class TestPrimaryKeyFormat:
    def test_scheme_pk_keeps_legacy_format(self):
        assert build_item_pk("p1", "projectBasicInfo") == "p1_projectBasicInfo"
        assert build_item_pk("p1", "projectBasicInfo", "scheme") == "p1_projectBasicInfo"

    def test_empty_and_none_domain_fail_closed_to_scheme(self):
        """域名缺失/空串必须视同 scheme —— 绝不静默落成新格式主键。"""
        for bad in ("", None):
            assert build_item_pk("p1", "projectInfo", bad) == "p1_projectInfo"

    def test_bid_response_pk_format(self):
        assert build_item_pk("p1", "projectInfo", "bid_response") == \
            "p1__bid_response__projectInfo"

    def test_parse_roundtrip(self):
        assert parse_item_pk(
            build_item_pk("p1", "projectInfo", "bid_response")) == \
            ("p1", "projectInfo", "bid_response")
        assert parse_item_pk("p1_projectBasicInfo") == \
            ("p1", "projectBasicInfo", "scheme")
        assert parse_item_pk("") == ("", "", "scheme")
        assert parse_item_pk("_") == ("", "", "scheme")

    def test_scheme_pk_with_underscore_project_id(self):
        """scheme 域主键用 rfind('_') 反解，project_id 含下划线也必须正确。"""
        pk = build_item_pk("proj_001", "projectBasicInfo")
        assert parse_item_pk(pk) == ("proj_001", "projectBasicInfo", "scheme")

    def test_update_item_status_uses_build_item_pk(self):
        """静态护栏：_update_item_status 必须经 build_item_pk 构造主键。"""
        src = inspect.getsource(ba._update_item_status)
        assert "build_item_pk(" in src, "必须经 build_item_pk 唯一出口构造主键"
        assert 'f"{project_id}_{item_id}"' not in src, "禁止裸主键拼接"


class TestNoBarePkConstructionInRouter:
    """全仓静态扫描：路由层不得再出现 scheme 域的裸主键拼接。

    ⚠️ 这是本仓反复踩的「同一契约两处各自实现」陷阱（§4.3/§4.7/§4.13/§4.14）：
    主键格式一旦在 2 处各自拼接，改一处就分叉 —— scheme 域历史数据与
    bid_response 域新数据会互相串。
    """

    def test_router_has_no_bare_pk_fstring(self):
        tree = ast.parse(_BA_SRC.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value for v in node.values if isinstance(v, ast.Constant))
                if "project_id}_{item_id}" in text:
                    offenders.append(node.lineno)
        assert not offenders, (
            "路由层仍有裸主键拼接（行 %s），必须改用 build_item_pk" % offenders)


# =========================================================================
# 三、提取内容标准（缺失标注规范单一出口）
# =========================================================================
class TestContentStandards:
    """对齐易标 jsonTask 的 3 条约束 + Markdown 整体无结果规则。

    核心不变量：**整项无内容 → 只返回「未提取到」**；**局部缺失 → 写「没有提及」**。
    两者不可混用，否则 is_missing_result 无法区分「真缺失」与「部分缺失」。
    """

    def test_build_task_prompt_markdown_appends_rule_once(self):
        prompt = build_task_prompt("procurementList", template_body="任务：提取采购清单。")
        assert prompt.count("整体无结果规则") == 1, "整体无结果规则只能追加一次"
        assert MARKDOWN_MISSING_RESULT in prompt
        assert PARTIAL_MISSING_TEXT in prompt

    def test_build_task_prompt_markdown_default_body(self):
        prompt = build_task_prompt("procurementList")
        assert prompt.startswith("任务：")
        assert MARKDOWN_MISSING_RESULT in prompt

    def test_build_task_prompt_json_has_three_constraints(self):
        prompt = build_task_prompt("projectInfo")
        assert "输出格式必须为 JSON" in prompt
        assert "禁止修改 key 和结构" in prompt
        assert PARTIAL_MISSING_TEXT in prompt
        # 键结构必须来自字段字典
        assert '"project_name"' in prompt
        assert '"project_address"' in prompt

    def test_build_task_prompt_json_goals_from_description(self):
        prompt = build_task_prompt("projectInfo")
        assert "项目名称、编号、类型、预算和地址" in prompt

    def test_build_task_prompt_json_explicit_body_overrides_template(self):
        prompt = build_task_prompt("projectInfo", template_body="自定义模板")
        assert "自定义模板" in prompt
        assert '"project_name"' not in prompt

    def test_build_json_template_matches_fields_dict(self):
        """JSON 模板的键集合必须与字段字典逐键一致（唯一事实源）。"""
        for item in get_items_by_domain("bid_response"):
            fields = get_item_fields(item["item_id"])
            if not fields:
                assert build_json_template(item["item_id"]) == "{}"
                continue
            tpl = json.loads(build_json_template(item["item_id"]))
            assert set(tpl.keys()) == {k for k, _ in fields}
            for k, label in fields:
                assert tpl[k] == label

    def test_markdown_items_have_no_fields(self):
        for item in get_items_by_domain("bid_response"):
            if item["output_type"] == "markdown":
                assert get_item_fields(item["item_id"]) == []

    def test_partial_vs_full_missing_are_distinct(self):
        """整项标记与局部标记必须逐字不同 —— 否则 is_missing_result 无法区分。"""
        assert MARKDOWN_MISSING_RESULT != PARTIAL_MISSING_TEXT
        assert MARKDOWN_MISSING_RESULT == "未提取到"
        assert PARTIAL_MISSING_TEXT == "没有提及"


# =========================================================================
# 四、技术评分项专判（对齐易标 isMissingTechnicalScoreItems）
# =========================================================================
class TestTechnicalScoreItems:
    """技术评分项小节是技术方案编制的主要依据：它缺失时用户会带着空白进入
    目录生成，而 techRequirements 整体不算「未提取到」（技术评分要求小节可能有
    内容）。故必须单独判定该小节。
    """

    def _doc(self, section_body: str, extra: str = "") -> str:
        return (f"## {TECH_SCORE_ITEMS_HEADING}\n\n{section_body}\n\n"
                f"## 技术评分要求\n\n{extra or 'y'}")

    def test_section_body_partial_missing_is_missing(self):
        assert is_missing_technical_score_items(
            self._doc(PARTIAL_MISSING_TEXT)) is True

    def test_section_body_with_content_is_not_missing(self):
        assert is_missing_technical_score_items(self._doc("- 方案合理性：10 分")) is False

    def test_whole_item_missing_is_missing(self):
        assert is_missing_technical_score_items(MARKDOWN_MISSING_RESULT) is True

    def test_no_section_is_not_missing(self):
        """整项无该小节 → 不算「小节缺失」（无锚点即无从判定，不误报）。"""
        assert is_missing_technical_score_items("其它内容") is False

    def test_empty_and_none_input(self):
        assert is_missing_technical_score_items("") is False
        assert is_missing_technical_score_items(None) is False

    def test_section_truncated_by_next_heading(self):
        """小节体必须止于下一个标题，不能吞掉后续小节的内容。"""
        doc = "## 技术评分项\n\n没有提及\n\n# 后续正文\n\n有内容"
        assert is_missing_technical_score_items(doc) is True

    def test_crlf_line_endings(self):
        doc = "## 技术评分项\r\n\r\n没有提及\r\n\r\n## 技术评分要求\r\n\r\nz"
        assert is_missing_technical_score_items(doc) is True


# =========================================================================
# 五、分段策略（对齐 userTextSplitter.cjs）
# =========================================================================
class TestSegmentStrategy:
    """均分分段策略：默认关闭，开启后不得丢内容 / 尾段塌缩 / 切断围栏。"""

    def _long_text(self) -> str:
        # 混合自然边界，覆盖 5 组边界优先级（标题/空行、换行、句末、分号、逗号）
        return ("# 一、总则\n" + "甲。" * 200 + "\n\n"
                + "乙，乙，乙；丙。" * 150 + "\n\n"
                + "# 二、细则\n" + "丁，丁，丁。" * 250 + "\n") * 40

    def test_default_matches_old_sliding_window_exactly(self):
        """默认参数下行为必须与旧滑动窗口**逐字一致**（向后兼容护栏）。"""
        text = self._long_text()
        explicit_default = split_for_analysis(text)
        explicit_sliding = split_for_analysis(text, chunk_size=16000, overlap=500)
        assert explicit_default == explicit_sliding, "默认行为被改动了"

    def test_even_mode_keeps_all_content(self):
        """均分模式无 overlap，拼回必须与原文逐字符相等 —— 不得丢字。"""
        text = self._long_text()
        segments = split_for_analysis(text, even=True, context_length_limit=2000)
        assert "".join(segments) == text

    def test_sliding_window_mode_loses_overlap_bytes(self):
        """滑动窗口模式按设计有 overlap（concat > 原文），用于锁定语义差异。"""
        text = self._long_text()
        sliding = split_for_analysis(text, chunk_size=2000, overlap=500)
        assert sum(len(s) for s in sliding) > len(text)

    def test_even_mode_falls_back_when_limit_zero(self):
        """context_length_limit<=0 必须回退旧滑动窗口（避免 0 段上限的退化切分）。"""
        text = self._long_text()
        for bad in (0, -1):
            assert split_for_analysis(text, even=True, context_length_limit=bad) == \
                split_for_analysis(text, chunk_size=16000, overlap=500)

    def test_even_mode_falls_back_when_below_limit(self):
        """文本短于段上限 → 单段原样返回；空文本 → 空清单。"""
        assert split_for_analysis("短文本", even=True,
                                  context_length_limit=1000) == ["短文本"]
        assert split_for_analysis("", even=True, context_length_limit=1000) == []

    def test_even_mode_no_tail_collapse(self):
        """尾段不得塌缩：所有段长都必须在目标段长的 35% 以上。

        这是易标 canUseCandidate 的「剩余量校验」要解决的问题 —— 滑动窗口
        会让最后一段只剩零头，AI 各段负载严重不均。
        """
        text = self._long_text()
        limit = 2000
        segment_limit = max(1, int(limit * 0.8))
        segment_count = max(1, -(-len(text) // segment_limit))
        target = max(1, -(-len(text) // segment_count))
        min_len = max(1, int(target * 0.35))
        segments = split_for_analysis(text, even=True, context_length_limit=limit)
        short = [(i, len(s)) for i, s in enumerate(segments) if len(s) < min_len]
        assert not short, f"存在塌缩段（min={min_len}）：{short[:5]}"

    def test_even_mode_segment_count_matches_formula(self):
        """段数必须等于 ceil(总长 / (上限 × 0.8)) —— 与易标公式一致。"""
        text = self._long_text()
        limit = 2000
        segment_limit = max(1, int(limit * 0.8))
        expected = max(1, -(-len(text) // segment_limit))
        assert len(split_for_analysis(text, even=True,
                                      context_length_limit=limit)) == expected

    def test_even_mode_never_splits_fence(self):
        """任何切点都不得落在代码围栏内部（否则表格/代码块被切断）。"""
        block = "```mermaid\nflowchart TD\n  A-->B\n  B-->C\n```"
        filler = "甲。" * 300
        text = filler + "\n\n" + block + "\n\n" + filler
        for limit in (1000, 600, 400):
            segments = split_for_analysis(text, even=True, context_length_limit=limit)
            assert "".join(segments) == text
            for seg in segments:
                assert seg.count("```") % 2 == 0, f"围栏被切断：{seg[:80]!r}"

    def test_surrogate_pair_protection(self):
        """切点落在代理对中间必须顺延 1 个字符 —— 否则产生孤立代理对，
        下游 .encode('utf-8') 直接 UnicodeEncodeError。"""
        text = "\ud83d\ude00"  # 😀
        assert svc._avoids_surrogate_pair(text, 1) == 2
        assert svc._avoids_surrogate_pair(text, 0) == 0
        assert svc._avoids_surrogate_pair(text, 2) == 2
        assert svc._avoids_surrogate_pair("abcd", 1) == 1

    def test_ratio_constants_match_reference(self):
        """分段比例必须与易标 userTextSplitter.cjs 逐值一致。

        参考实现常量：DEFAULT_CONTEXT_LENGTH_LIMIT=400000、
        DEFAULT_CONTEXT_LIMIT_RATIO=0.8、STRICT_WINDOW_RATIO=0.12、
        RELAXED_WINDOW_RATIO=0.25、MIN_SEGMENT_RATIO=0.35、MAX_SEGMENT_RATIO=1.1。
        """
        assert svc.SEGMENT_LIMIT_RATIO == 0.8
        assert svc.SEGMENT_STRICT_RATIO == 0.12
        assert svc.SEGMENT_RELAXED_RATIO == 0.25
        assert svc.SEGMENT_MIN_RATIO == 0.35
        assert svc.SEGMENT_MAX_RATIO == 1.1
        assert svc.SEGMENT_CONTEXT_LENGTH_LIMIT == 0  # 0 = 默认不启用均分

    def test_boundary_priority_group_count(self):
        """5 组边界优先级必须保持（标题/空行 > 换行 > 句末 > 分号 > 逗号）。"""
        assert len(svc._BOUNDARY_GROUPS) == 5

    def test_split_even_is_config_driven_wrapper(self):
        """_split_tender_text 是唯一入口：关闭走滑动窗口，开启走均分，
        limit<=0 时即便开关打开也回退（避免退化切分）。"""
        from app.config import settings
        text = ("# 一、总则\n" + "甲。" * 200 + "\n\n"
                + "乙，乙，乙。" * 150 + "\n") * 40
        orig_even = settings.bid_analysis_segment_even
        orig_limit = settings.bid_analysis_segment_context_limit
        try:
            settings.bid_analysis_segment_even = False
            assert ba._split_tender_text(text) == split_for_analysis(text)

            settings.bid_analysis_segment_even = True
            settings.bid_analysis_segment_context_limit = 2000
            assert ba._split_tender_text(text) == split_for_analysis(
                text, even=True, context_length_limit=2000)

            settings.bid_analysis_segment_context_limit = 0
            assert ba._split_tender_text(text) == split_for_analysis(text)
        finally:
            settings.bid_analysis_segment_even = orig_even
            settings.bid_analysis_segment_context_limit = orig_limit

    def test_concurrency_helpers_are_config_driven(self):
        """并发/重试/预算必须来自配置，且下限保护（不得为 0 导致死锁或空转）。"""
        from app.config import settings
        try:
            assert ba._item_concurrency() == 2
            assert ba._segment_concurrency() == 3
            assert ba._item_retries() == 2
            settings.bid_analysis_item_concurrency = 0
            assert ba._item_concurrency() == 1, "并发下限必须为 1"
            settings.bid_analysis_segment_concurrency = -1
            assert ba._segment_concurrency() == 1
            settings.bid_analysis_item_retries = -5
            assert ba._item_retries() == 0, "重试下限必须为 0"
            assert ba._MAX_DOC_CHARS == settings.bid_analysis_segment_budget
        finally:
            settings.bid_analysis_item_concurrency = 2
            settings.bid_analysis_segment_concurrency = 3
            settings.bid_analysis_item_retries = 2
            settings.bid_analysis_segment_budget = 30000


# =========================================================================
# 六、断点续跑（对齐易标 tasksToRun 的 status!=='success' 过滤）
# =========================================================================
async def _seed_project(db, pid: str) -> str:
    """建一个项目 + 方案，返回 scheme_id。"""
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (sid, pid, "s"))
    await db.commit()
    return sid


async def _seed_item(db, pid: str, item_id: str, status: str = "success",
                     domain: str = "scheme") -> None:
    """按域主键插入一条解析项行（模拟上一轮已落库的结果）。"""
    def_ = get_item_def(item_id) or {}
    pk = build_item_pk(pid, item_id, domain)
    await db.execute(
        "INSERT OR REPLACE INTO bid_analysis_items "
        "(id, project_id, scheme_id, item_id, label, output_type, required, "
        "status, content, error, sort_order, domain) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pk, pid, "", item_id, def_.get("label", item_id),
         def_.get("output_type", "markdown"), int(def_.get("required", 0)),
         status, "内容", "", int(def_.get("sort_order", 0)), domain))
    await db.commit()


class TestSkipDone:
    """断点续跑：默认关闭；开启后跳过已成功项；force_rerun / mode=item 恒忽略；
    查库异常必须 fail-open（全量执行），绝不因查库失败静默跳过重跑。
    """

    @pytest.mark.asyncio
    async def test_skip_done_default_off(self, db_conn):
        db = db_conn
        pid = uuid.uuid4().hex
        await _seed_project(db, pid)
        await _seed_item(db, pid, "projectBasicInfo")
        cfg = AnalysisConfig(mode="key", skip_done=False)
        # skip_done=False 时必须全量执行（与旧版一致）
        assert len(await cfg.get_task_items_async(db, pid)) == len(REQUIRED_ITEM_IDS)

    @pytest.mark.asyncio
    async def test_skip_done_skips_success_items(self, db_conn):
        db = db_conn
        pid = uuid.uuid4().hex
        await _seed_project(db, pid)
        await _seed_item(db, pid, "projectBasicInfo", "success")
        await _seed_item(db, pid, "safetyMeasures", "error")  # 失败项不得跳过
        cfg = AnalysisConfig(mode="key", skip_done=True)
        ids = [it["item_id"] for it in await cfg.get_task_items_async(db, pid)]
        assert "projectBasicInfo" not in ids, "已成功的项必须被跳过"
        assert "safetyMeasures" in ids, "失败项必须重跑"
        assert len(ids) == len(REQUIRED_ITEM_IDS) - 1

    @pytest.mark.asyncio
    async def test_skip_done_ignored_on_force_rerun(self, db_conn):
        """force_rerun 语义 = 强制全量重跑，不得被 skip_done 短路。"""
        db = db_conn
        pid = uuid.uuid4().hex
        await _seed_project(db, pid)
        await _seed_item(db, pid, "projectBasicInfo", "success")
        cfg = AnalysisConfig(mode="key", skip_done=True, force_rerun=True)
        items = await cfg.get_task_items_async(db, pid)
        assert [it["item_id"] for it in items] == REQUIRED_ITEM_IDS

    @pytest.mark.asyncio
    async def test_skip_done_ignored_on_item_mode(self, db_conn):
        """单项重跑必须严格按勾选执行 —— 否则「重跑 1 项」会变成空操作。"""
        db = db_conn
        pid = uuid.uuid4().hex
        await _seed_project(db, pid)
        await _seed_item(db, pid, "resourceAllocation", "success")
        cfg = AnalysisConfig(mode="item", selected_item_ids=["resourceAllocation"],
                            skip_done=True)
        items = await cfg.get_task_items_async(db, pid)
        assert [it["item_id"] for it in items] == ["resourceAllocation"]


    @pytest.mark.asyncio
    async def test_skip_done_fails_open_on_db_error(self, db_conn):
        u"""查库异常必须 fail-open 为全量执行 —— 绝不因查库失败静默跳过重跑。"""
        db = db_conn
        pid = uuid.uuid4().hex
        await _seed_project(db, pid)
        await _seed_item(db, pid, u'projectBasicInfo', u'success')

        class _BrokenDb:
            async def execute(self, *a, **k):
                raise RuntimeError(u'模拟数据库不可用')

        cfg = AnalysisConfig(mode=u'key', skip_done=True)
        items = await cfg.get_task_items_async(_BrokenDb(), pid)
        assert [it[u'item_id'] for it in items] == REQUIRED_ITEM_IDS

    @pytest.mark.asyncio
    async def test_skip_done_requires_db_and_project_id(self, db_conn):
        u"""缺 db 或 project_id 时不得跳过（等价于未开启）。"""
        cfg = AnalysisConfig(mode=u'key', skip_done=True)
        assert [it[u'item_id'] for it in cfg.get_task_items()] == REQUIRED_ITEM_IDS
        # 缺 project_id：查询直接短路 → 全量
        assert [it[u'item_id'] for it in
                await cfg.get_task_items_async(db_conn, u'')] == REQUIRED_ITEM_IDS
        # 缺 db：同样短路
        assert [it[u'item_id'] for it in
                await cfg.get_task_items_async(None, u'p1')] == REQUIRED_ITEM_IDS

    @pytest.mark.asyncio
    async def test_skip_done_domain_isolated(self, db_conn):
        u"""域间隔离：两域的成功项互不影响。"""
        db = db_conn
        pid = uuid.uuid4().hex
        await _seed_project(db, pid)
        await _seed_item(db, pid, u'projectBasicInfo', u'success', u'scheme')
        await _seed_item(db, pid, u'projectInfo', u'success', u'bid_response')

        scheme_ids = [it[u'item_id'] for it in
                      await AnalysisConfig(mode=u'key', skip_done=True)
                      .get_task_items_async(db, pid)]
        assert u'projectBasicInfo' not in scheme_ids

        br_ids = [it[u'item_id'] for it in
                  await AnalysisConfig(mode=u'key', domain=u'bid_response',
                                       skip_done=True)
                  .get_task_items_async(db, pid)]
        assert u'projectInfo' not in br_ids
        assert u'projectBasicInfo' not in br_ids, u'scheme 域项不得出现在 bid_response 域'

    def test_skip_done_query_uses_domain_column(self):
        u"""续跑查询必须带 domain 条件（静态护栏）。"""
        src = inspect.getsource(fetch_success_item_ids)
        assert u'domain=?' in src, u'查询必须按域过滤，否则两域成功项互相串'
        assert u"status='success'" in src


# =========================================================================
# 七、路由契约
# =========================================================================
class TestRouteContract:
    @pytest.mark.asyncio
    async def test_items_default_returns_scheme_shape(self, db_conn):
        u"""默认无参调用必须与旧版契约逐字一致（前端依赖）。"""
        res = await ba.list_analysis_items()
        # ✅ 2026-09-30 第十六轮：新增 techScoring（可选）后的新口径。
        #    必选仍 17 项；可选由 1 增至 2；分组由 13 增至 14。
        assert res[u'total'] == 19
        assert res[u'required_count'] == 17
        assert res[u'optional_count'] == 2
        assert res[u'group_count'] == 14
        assert res[u'required_item_ids'] == REQUIRED_ITEM_IDS
        assert res[u'items'] == [dict(it) for it in ANALYSIS_ITEMS]
        assert res[u'groups'] == ba.get_groups()
        assert res[u'domain'] == u'scheme'
        assert res[u'domain_unknown'] is False

    @pytest.mark.asyncio
    async def test_items_domain_unknown_fails_closed(self, db_conn):
        u"""未知域必须返回空清单 + domain_unknown，不得静默回退到 scheme。"""
        res = await ba.list_analysis_items(u'no_such_domain')
        assert res[u'items'] == [] and res[u'groups'] == []
        assert res[u'total'] == 0 and res[u'domain_unknown'] is True
        assert res[u'domain'] == u'no_such_domain'

    @pytest.mark.asyncio
    async def test_items_bid_response_disabled_returns_404(self, db_conn, monkeypatch):
        u"""招标响应域未启用时必须 404，而非返回一套无法执行的清单。"""
        from app.config import settings
        from fastapi import HTTPException
        monkeypatch.setattr(settings, u'bid_response_domain_enabled', False, raising=False)
        with pytest.raises(HTTPException) as exc:
            await ba.list_analysis_items(u'bid_response')
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_items_bid_response_enabled_after_flag(self, db_conn, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, u'bid_response_domain_enabled', True, raising=False)
        res = await ba.list_analysis_items(u'bid_response')
        assert res[u'total'] == 18
        assert res[u'required_count'] == 6
        assert res[u'group_count'] == 11
        assert res[u'domain'] == u'bid_response'
        assert res[u'domain_unknown'] is False
        assert res[u'required_item_ids'] == [
            u'projectOverview', u'techRequirements', u'projectInfo',
            u'partAInfo', u'deliveryAndServiceRequirements',
            u'responseFileRequirements']

    @pytest.mark.asyncio
    async def test_items_single_item_additive_keys(self, db_conn):
        u"""单项定义加法式新增 domain / fields 两键，旧字段全部保留。"""
        res = await ba.get_analysis_item(u'projectInfo')
        assert res[u'domain'] == u'bid_response'
        assert res[u'fields'] == get_item_fields(u'projectInfo')
        assert res[u'item_id'] == u'projectInfo'
        assert res[u'label'] == u'项目信息'
        assert res[u'required'] == 1

    @pytest.mark.asyncio
    async def test_items_single_item_cross_domain_404(self, db_conn):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await ba.get_analysis_item(u'projectInfo', domain=u'scheme')
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_items_single_item_unknown_404(self, db_conn):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await ba.get_analysis_item(u'no_such_item')
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_domains_endpoint_reflects_setting(self, db_conn, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, u'bid_response_domain_enabled', False, raising=False)
        res = await ba.list_extraction_domains()
        by_name = {d[u'domain']: d for d in res[u'domains']}
        assert by_name[u'scheme'][u'enabled'] is True
        assert by_name[u'scheme'][u'total'] == 19
        assert by_name[u'scheme'][u'required_count'] == 17
        assert by_name[u'bid_response'][u'enabled'] is False
        assert by_name[u'bid_response'][u'total'] == 18
        assert by_name[u'bid_response'][u'required_count'] == 6

        monkeypatch.setattr(settings, u'bid_response_domain_enabled', True, raising=False)
        res2 = await ba.list_extraction_domains()
        assert {d[u'domain']: d[u'enabled'] for d in res2[u'domains']} == {
            u'scheme': True, u'bid_response': True}

    @pytest.mark.asyncio
    async def test_domains_endpoint_never_404(self, db_conn):
        u"""域清单端点不得抛异常（前端据此渲染域切换器）。"""
        res = await ba.list_extraction_domains()
        assert isinstance(res[u'domains'], list) and len(res[u'domains']) == 2


class TestDomainMigration:
    u"""domain 列的迁移与旧库兼容（旧数据零迁移）。"""

    @pytest.mark.asyncio
    async def test_domain_column_exists_after_migrate(self, db_conn):
        cur = await db_conn.execute(u'PRAGMA table_info(bid_analysis_items)')
        names = {dict(r)[u'name'] for r in await cur.fetchall()}
        assert u'domain' in names, u'_migrate 必须为 bid_analysis_items 补 domain 列'

    @pytest.mark.asyncio
    async def test_domain_column_default_is_scheme(self, db_conn):
        u"""不带 domain 的 INSERT 必须落 scheme（旧数据零迁移）。"""
        await db_conn.execute(
            u'INSERT INTO bid_analysis_items (id, project_id, item_id) VALUES (?,?,?)',
            (u'p1_x', u'p1', u'x'))
        await db_conn.commit()
        cur = await db_conn.execute(
            u'SELECT domain FROM bid_analysis_items WHERE id=?', (u'p1_x',))
        row = await cur.fetchone()
        assert row is not None and row[u'domain'] == u'scheme'

    def test_schema_sql_declares_domain_column(self):
        from app.schema_sql import SCHEMA_SQL
        assert "domain TEXT DEFAULT 'scheme'" in SCHEMA_SQL
        assert "idx_bid_analysis_project_domain" in SCHEMA_SQL

    @pytest.mark.asyncio
    async def test_update_item_status_writes_domain(self, db_conn):
        u"""AI 写路径写域，scheme 域主键保持旧格式。"""
        pid = uuid.uuid4().hex
        await _seed_project(db_conn, pid)
        await ba._update_item_status(
            db_conn, pid, u'projectBasicInfo', u'success', content=u'内容')
        cur = await db_conn.execute(
            u'SELECT id, domain FROM bid_analysis_items WHERE project_id=?', (pid,))
        row = await cur.fetchone()
        assert row is not None
        assert row[u'id'] == u'%s_projectBasicInfo' % pid, u'scheme 域主键格式被改动了'
        assert row[u'domain'] == u'scheme'

    @pytest.mark.asyncio
    async def test_update_item_status_auto_derives_domain(self, db_conn):
        u"""不传 domain 时按 item_id 自动派生（6 处调用点零改动也正确落库）。"""
        pid = uuid.uuid4().hex
        await _seed_project(db_conn, pid)
        await ba._update_item_status(
            db_conn, pid, u'projectInfo', u'success', content=u'{}')
        cur = await db_conn.execute(
            u'SELECT id, domain FROM bid_analysis_items WHERE project_id=?', (pid,))
        row = await cur.fetchone()
        assert row is not None
        assert row[u'id'] == u'%s__bid_response__projectInfo' % pid
        assert row[u'domain'] == u'bid_response'


# =========================================================================
# 八、下游「提取即消费」
# =========================================================================
class TestDownstreamConsumption:
    u"""format_downstream_context 必须覆盖两域全部有效项（AGENTS.md §4.4）。"""

    @staticmethod
    def _item(item_id, content, status=u'success', output_type=u'markdown'):
        def_ = get_item_def(item_id) or {}
        return {u'item_id': item_id, u'label': def_.get(u'label', item_id),
                u'status': status, u'content': content,
                u'output_type': output_type}

    def test_scheme_domain_order_unchanged(self):
        u"""scheme 域输出顺序必须与旧版（ANALYSIS_ITEMS 顺序）逐字一致。"""
        items = {}
        for it in ANALYSIS_ITEMS:
            items[it[u'item_id']] = self._item(it[u'item_id'], u'内容-' + it[u'item_id'])
        out = svc.format_downstream_context(items)
        order = [ln.split(u'## ')[-1] for ln in out.split(u'\n')
                 if ln.startswith(u'## ')]
        assert order == [it[u'label'] for it in ANALYSIS_ITEMS]

    def test_bid_response_items_are_emitted(self):
        u"""招标响应域的项也必须被下发（否则下游拿不到项目信息等）。"""
        items = {u'projectInfo': self._item(
            u'projectInfo', u'{"project_name": "XX工程"}', output_type=u'json')}
        out = svc.format_downstream_context(items)
        assert u'项目信息' in out
        assert u'XX工程' in out

    def test_missing_result_not_emitted(self):
        u"""缺失项不得下发（沿用 is_missing_result 唯一口径）。"""
        items = {u'projectBasicInfo': self._item(
            u'projectBasicInfo', MARKDOWN_MISSING_RESULT)}
        out = svc.format_downstream_context(items)
        assert u'项目级基本信息' not in out

    def test_unknown_item_id_fallback_still_emitted(self):
        u"""不在任何域清单内的脏数据项仍兜底下发（历史行为不变）。"""
        items = {u'legacy_unknown': self._item(u'legacy_unknown', u'历史脏数据')}
        out = svc.format_downstream_context(items)
        assert u'历史脏数据' in out

    def test_empty_items_yields_header_only(self):
        out = svc.format_downstream_context({})
        assert u'提取项目结果' in out
