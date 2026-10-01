"""跨章节重复检测（G10）+ CON-06 接线回归护栏（2026-10-01）。

覆盖：
  1. 归一化与骨架键（NFKC 折叠 / 标点剔除 / 数字·日期·标准号·百分数占位符 /
     规则顺序 / 单位边界刻意不修）
  2. Dice / containment / 双阈值分档
  3. 两级判定（exact 骨架键 / skeleton 双阈值；**单阈值不达标必须不判**）
  4. 句子切分（句末切、逗号不切、短句过滤、去重、上限）
  5. 跨章节搬运检测（exact 组 / exclude_pairs / claimed 防双报 / 截断标记 /
     脏数据容忍）
  6. 标题近似雷同（阈值 / 同名跳过 / 过短跳过）
  7. CON-06 接线（注册表健康 / 派生编号 / 与 CON-05 不双报 / 严重度升级 /
     规则版本已 bump）
  8. 目录侧接线（similar_titles 为加法式附加键，**不改 ok / issue_counts**）

测试策略：纯函数断言 + 端到端 run_preflight，零 AI、零 DB。
"""
from __future__ import annotations

import ast
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DD_PATH = os.path.join(BACKEND, "app", "services", "duplicate_detection.py")
PE_PATH = os.path.join(BACKEND, "app", "services", "preflight_engine.py")
OQ_PATH = os.path.join(BACKEND, "app", "services", "outline_quality.py")
AR_PATH = os.path.join(BACKEND, "app", "services", "audit_rules.py")


def _src(path: str) -> str:
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _fn(path: str, name: str) -> str:
    """取模块顶层**函数**的源码片段（静态护栏用，避免行号漂移）。"""
    src = _src(path)
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"未找到函数 {name} in {path}")


def _body(path: str, name: str) -> str:
    """取函数**函数体**源码（剔除 docstring）。

    ⚠️ 静态护栏必须只看代码、不能看注释：docstring 里为了说明设计取舍
    必然会提到被禁止的标识符（如「排除集合复用 _find_duplicates」），
    直接对整段函数做子串断言会恒假失败。
    """
    src = _src(path)
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            doc = ast.get_docstring(node, clean=False)
            text = ast.get_source_segment(src, node) or ""
            if doc:
                text = text.replace(doc, "", 1)
            return text
    raise AssertionError(f"未找到函数 {name} in {path}")


def _assigned(path: str, target: str) -> str:
    """取模块顶层「赋值」语句的源码片段（如 ``_CONSISTENCY_RULES = (...)``）。"""
    src = _src(path)
    for node in ast.parse(src).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = ([t.id for t in node.targets if isinstance(t, ast.Name)]
                     if isinstance(node, ast.Assign)
                     else [node.target.id if isinstance(node.target, ast.Name) else ""])
            if target in names:
                return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"未找到顶层赋值 {target} in {path}")


from app.services import duplicate_detection as dd  # noqa: E402
from app.services.audit_rules import (  # noqa: E402
    _PROGRAM_EMITTED_RULE_IDS, RULE_VERSION, get_rule, validate_rule_registry,
)
from app.services.outline_quality import check_outline_continuity  # noqa: E402
from app.services.preflight_engine import (  # noqa: E402
    PreflightContext, _find_duplicates, check_duplication, run_preflight,
)


def _sections(*items):
    """把 ``(id, title, content)`` 快速展开成 PreflightContext 可吃的章节列表。"""
    out = []
    for i, (sid, title, content) in enumerate(items):
        out.append({"id": sid, "title": title, "content": content,
                    "word_count": len(content), "parent_id": "",
                    "sort_order": i})
    return out


# ---------------------------------------------------------------------------
# 1. 归一化与骨架键
# ---------------------------------------------------------------------------

def test_normalize_nfkc_folds_fullwidth():
    assert dd.normalize_comparable("３ｍ") == "3m"
    assert dd.normalize_comparable("ＧＢＴ５０５０２") == "GBT50502"


def test_normalize_strips_space_punct_and_symbols():
    assert dd.normalize_comparable("基坑深度 3m，边坡 1:0.75；（一级）") == "基坑深度3m边坡1075一级"


def test_normalize_empty_and_none():
    assert dd.normalize_comparable("") == ""
    assert dd.normalize_comparable(None) == ""


def test_skeleton_number_becomes_placeholder():
    a = dd.build_skeleton_key("基坑开挖深度为3m，边坡坡度1:0.75")
    b = dd.build_skeleton_key("基坑开挖深度为5m，边坡坡度1:0.75")
    assert a == b, "仅数字不同的段落必须归一到同一骨架键（这是 G10 的核心价值）"
    assert "{num}" in a


def test_skeleton_date_and_standard_code():
    assert dd.build_skeleton_key("2026年9月30日") == "{date}"
    assert dd.build_skeleton_key("2026年9月") == "{date}"
    assert dd.build_skeleton_key("2026-09-30") == "{date}"
    assert dd.build_skeleton_key("GB 50202-2018") == "{code}"
    assert dd.build_skeleton_key("JGJ 120-2012") == "{code}"
    assert dd.build_skeleton_key("GB/T 50502") == "{code}"


def test_skeleton_percent_money_page():
    assert dd.build_skeleton_key("合格率达到95%") == "合格率达到{percent}"
    assert dd.build_skeleton_key("造价约300万元") == "造价约{money}"
    assert dd.build_skeleton_key("见 P12") == "见{page}"


def test_skeleton_placeholder_braces_survive_cleanup():
    """占位符花括号必须保留，否则 {num} 退化成 num、无法与正文英文区分。"""
    sk = dd.build_skeleton_key("深度3m")
    assert sk == "深度{num}m"
    # 且幂等：对已归一化的骨架键再归一一次，结果不变
    assert dd.build_skeleton_key(sk) == sk


def test_skeleton_rule_order_date_not_eaten_by_number():
    """日期规则必须早于裸数字规则，否则日期会变成 {num}年{num}月 的残片。"""
    assert "{num}年" not in dd.build_skeleton_key("计划开工日期为2026年9月")
    assert dd.build_skeleton_key("2026年9月30日") == "{date}"


def test_skeleton_unit_normalization_is_deliberately_out_of_scope():
    """「6m」与「6000mm」不视为同骨架 —— 单位口径收敛是独立缺口。

    本仓已有三份互不相同的单位表（preflight_engine / facts_classification /
    content_standard），本模块故意不新增第四份；否则单位口径会再多一处分叉。
    """
    assert dd.build_skeleton_key("深度6m") != dd.build_skeleton_key("深度6000mm")


# ---------------------------------------------------------------------------
# 2. 相似度度量
# ---------------------------------------------------------------------------

def test_dice_formula_and_empty():
    a, b = frozenset("abcd"), frozenset("abef")
    # 交集 2 → 2*2/(4+4) = 0.5
    assert dd.dice(a, b) == pytest.approx(0.5)
    assert dd.dice(frozenset(), b) == 0.0
    assert dd.dice(a, frozenset()) == 0.0
    assert dd.dice(frozenset("abcd"), frozenset("wxyz")) == 0.0


def test_containment_formula():
    # 短集 2 个元素全部被长集包含 → 1.0（对称 Dice 只有 0.5）
    assert dd.containment(frozenset("ab"), frozenset("abcdef")) == pytest.approx(1.0)
    assert dd.containment(frozenset(), frozenset("abc")) == 0.0


def test_bigrams_short_text_returns_empty():
    assert dd.char_bigrams("a") == frozenset()       # 不足 n
    assert dd.char_bigrams("") == frozenset()
    assert dd.char_bigrams("ab") == frozenset({"ab"})  # 恰好 n → 1 个 gram
    assert len(dd.char_bigrams("abcd")) == 3


def test_tiers_long_vs_short():
    _, c1, d1 = dd._tier_for(40)
    _, c2, d2 = dd._tier_for(16)
    # 长句用更宽松阈值，短句从严
    assert c1 < c2 and d1 < d2


# ---------------------------------------------------------------------------
# 3. 两级判定
# ---------------------------------------------------------------------------

def test_exact_skeleton_match_across_number_changes():
    m = dd.sentences_near_match(
        "基坑开挖深度为3m，边坡坡度1:0.75，采用GB 50202-2018执行",
        "基坑开挖深度为5m，边坡坡度1:0.75，采用GB 50204-2002执行",
    )
    assert m is not None
    assert m["reason"] == "exact"


def test_near_match_requires_both_thresholds():
    """关键护栏：只满足 containment 或只满足 Dice 都不判。

    长句偶然包含短句 → containment 高但 Dice 低；
    两个同领域短句用词高度重叠 → Dice 较高但 containment 不足。
    单阈值判定会把这两类都误报。
    """
    a = "基坑开挖深度为3m，边坡坡度1:0.75放坡，坡顶留1m宽压顶土"
    b = "边坡坡度按1:0.75放坡执行，坡度不得小于设计规定值"
    m = dd.sentences_near_match(a, b)
    assert m is None, f"containment 高但 Dice 低时不得判定为照抄：{m}"


def test_near_match_rejects_unrelated_content():
    assert dd.sentences_near_match(
        "基坑开挖深度为3m，边坡坡度1:0.75放坡，坡顶留1m宽压顶土",
        "脚手架立杆间距1.5m，步距1.8m，连墙件按两步三跨设置",
    ) is None


def test_near_match_rejects_short_sentences():
    """短句子 2-gram 集合过小，任何两句 Dice 都偏高 → 必须直接跳过。"""
    assert dd.sentences_near_match("基坑深度3m", "基坑深度5m") is None
    assert dd.sentences_near_match("基坑深度3m", "") is None
    assert dd.sentences_near_match("", "基坑深度3m") is None


def test_near_match_accepts_skeleton_tier():
    """骨架键不同但双阈值都达标 → 判 skeleton（近似照抄）。

    注意：仅改数字 / 标准号会命中 **exact**（骨架键相同），
    要触发 skeleton 必须让**中文用词**发生小幅变化。
    """
    base = ("基坑开挖深度为3m，边坡坡度1:0.75放坡，坡顶留1m宽压顶土，"
            "采用GB 50202-2018执行施工")
    variant = ("基坑开挖深度为3m，边坡坡度1:0.75放坡，坡顶留1m宽压顶土，"
               "按GB 50203-2002执行施工")
    m = dd.sentences_near_match(base, variant)
    assert m is not None
    assert m["reason"] == "skeleton"
    assert m["skeleton_a"] != m["skeleton_b"]
    assert m["dice"] >= m["tier_dice"] - 1e-9
    assert m["containment"] >= m["tier_cont"] - 1e-9


def test_sentence_similarity_returns_metrics():
    m = dd.sentence_similarity("基坑开挖深度为3m", "基坑开挖深度为5m")
    for key in ("skeleton_a", "skeleton_b", "dice", "containment",
                "length_ratio", "tier_cont", "tier_dice"):
        assert key in m
    assert m["length_ratio"] == pytest.approx(1.0)
    assert 0.0 <= m["dice"] <= 1.0


# ---------------------------------------------------------------------------
# 4. 句子切分
# ---------------------------------------------------------------------------

# 归一化后均 ≥ MIN_SENTENCE_CHARS(16) 的真实感句子（短于门槛会被过滤）
_S1 = "基坑开挖深度为3m，边坡坡度1:0.75放坡，坡顶留1m宽压顶土"
_S2 = "验收程序按现行规范要求逐条执行完成并及时归档保存"


def test_split_on_sentence_enders():
    text = _S1 + "。" + _S2 + "！？" + _S1 + "施工完毕"
    sents = dd.split_sentences(text)
    assert len(sents) == 3
    assert sents[0] == _S1
    assert sents[1] == _S2


def test_split_does_not_break_on_comma_or_ratio_colon():
    """分句与比值（坡度 1:0.75）不能切开 —— ASCII 冒号不是句末符。"""
    assert len(dd.split_sentences(_S1)) == 1
    assert len(dd.split_sentences("基坑深度3m，边坡采用1:0.75放坡")) == 1


def test_split_filters_short_and_dedups():
    text = "是。" + _S1 + "。" + _S1 + "。"
    sents = dd.split_sentences(text)
    assert len(sents) == 1, f"短句应被过滤、重复句应去重，实际：{sents}"


def test_split_empty_and_limits():
    assert dd.split_sentences("") == []
    assert dd.split_sentences(None) == []
    # 50 句互不相同 → 应被 max_sentences 截断到 5
    text = "。".join(f"{_S1}第{i}条工序要求" for i in range(50))
    assert len(dd.split_sentences(text, max_sentences=5)) == 5


def test_split_min_chars_is_configurable():
    """门槛可调（长文本用更大门槛，避免技术短语被当成可比对的句子）。"""
    assert len(dd.split_sentences("基坑深度3m", min_chars=8)) == 0
    assert len(dd.split_sentences(_S1, min_chars=40)) == 0
    assert len(dd.split_sentences(_S1, min_chars=4)) == 1


# ---------------------------------------------------------------------------
# 5. 跨章节搬运检测
# ---------------------------------------------------------------------------

def test_copies_exact_across_sections():
    dup = "基坑开挖深度为3m，边坡坡度1:0.75放坡，采用GB 50202-2018执行。"
    secs = _sections(
        ("a", "施工工艺", dup + "本章节描述其它独有的施工工艺流程。"),
        ("b", "安全保证", dup + "本章节描述安全组织与职责分工的不同内容。"),
        ("c", "验收要求", _S2 + "验收内容清单完整并归档。"),
    )
    res = dd.find_cross_section_copies(secs)
    assert res["groups"], "跨章节 exact 骨架相同必须被检出"
    g = res["groups"][0]
    assert g["reason"] == "exact"
    assert g["dice"] == 1.0
    assert g["section_pairs"][0][:2] in (("a", "b"), ("b", "a"))


def test_copies_exclude_pairs_suppresses_double_report():
    """exclude_pairs 必须真正压制报告 —— 否则同一现象被 CON-05/CON-06 双报。"""
    dup = "基坑开挖深度为3m，边坡坡度1:0.75放坡，采用GB 50202-2018执行。"
    secs = _sections(
        ("a", "施工工艺", dup + "本章节描述其它独有的施工工艺流程。"),
        ("b", "安全保证", dup + "本章节描述安全组织与职责分工的不同内容。"),
    )
    assert dd.find_cross_section_copies(secs)["groups"]
    # 无序对：正反序都必须生效
    for pair in (("a", "b"), ("b", "a")):
        res = dd.find_cross_section_copies(secs, exclude_pairs=[pair])
        assert not res["groups"], f"exclude_pairs={pair} 未生效"


def test_copies_no_double_report_between_two_levels():
    """exact 组占用的章节对被 claimed 记录，第二级 skeleton 不得重复报告。"""
    dup = "基坑开挖深度为3m，边坡坡度1:0.75放坡，采用GB 50202-2018执行。"
    secs = _sections(
        ("a", "施工工艺", dup + "本章节描述其它独有的施工工艺流程。"),
        ("b", "安全保证", dup + "本章节描述安全组织与职责分工的不同内容。"),
    )
    pairs = [tuple(g["section_pairs"][0][:2]) for g in
             dd.find_cross_section_copies(secs)["groups"]]
    assert len(pairs) == len(set(pairs)), "同一章节对被两个级别重复报告"


def test_copies_large_sentence_flagged():
    """归一化字数 ≥ COPY_GROUP_LARGE_CHARS 的搬运组必须标记 large。"""
    big = _S1 + "其余部分继续扩写以满足大段判定的最小字数要求。"
    secs = _sections(
        ("a", "施工工艺", big + "本章节描述其它独有的施工工艺流程。"),
        ("b", "安全保证", big + "本章节描述安全组织与职责分工的不同内容。"),
    )
    groups = dd.find_cross_section_copies(secs)["groups"]
    assert groups and any(g["large"] for g in groups), "大段照抄未被标记"


def test_copies_returns_all_keys_and_is_deterministic():
    secs = _sections(
        ("a", "施工工艺", _S1 + "补充说明A。"),
        ("b", "安全保证", _S1 + "补充说明B。"),
    )
    res = dd.find_cross_section_copies(secs)
    for key in ("groups", "truncated", "pairwise_compared"):
        assert key in res
    assert res == dd.find_cross_section_copies(secs), "同一输入必须得到同一结论"


def test_copies_tolerates_dirty_input():
    """缺键 / 空值 / 非 dict 元素一律容忍，不得抛异常。"""
    secs = _sections(
        ("a", "施工工艺", _S1 + "补充说明A。"),
        ("b", "", _S1 + "补充说明B。"),
    )
    secs.append({"title": "无 id 章节", "content": _S1})      # 缺 id → 跳过
    secs.append({"id": "c", "title": "空正文"})               # 空正文 → 跳过
    secs.append("not-a-dict")                                  # 非 dict → 跳过
    secs.append({"id": "d", "title": "无正文键"})              # 缺 content → 跳过
    res = dd.find_cross_section_copies(secs)
    assert res["groups"]


def test_copies_exclude_pairs_tolerates_garbage():
    """exclude_pairs 收到脏数据（None / 长度不对 / 空串）时不得抛异常。"""
    secs = _sections(("a", "施工工艺", _S1))
    res = dd.find_cross_section_copies(
        secs, exclude_pairs=[None, (), ("a",), ("", "b"), ("a", "b")])
    assert "groups" in res


# ---------------------------------------------------------------------------
# 6. 标题近似雷同
# ---------------------------------------------------------------------------

def test_title_similarity_known_values():
    assert dd.title_similarity("基坑降水与支护施工", "基坑降水及支护施工") > 0.70
    assert dd.title_similarity("安全保证措施", "应急处置措施") < 0.70


def test_title_similarity_short_and_empty():
    assert dd.title_similarity("短", "短乙") == 0.0
    assert dd.title_similarity("", "基坑降水") == 0.0
    assert dd.title_similarity("基坑降水", "") == 0.0


def test_find_similar_titles_reports_near_pairs():
    nodes = [
        {"path": "1.1", "title": "基坑降水与支护施工"},
        {"path": "1.2", "title": "基坑降水及支护施工"},
        {"path": "2.1", "title": "安全保证措施"},
    ]
    res = dd.find_similar_titles(nodes)
    assert len(res) == 1
    r = res[0]
    assert r["a_title"] == "基坑降水与支护施工"
    assert r["b_title"] == "基坑降水及支护施工"
    assert r["a_path"] == "1.1" and r["b_path"] == "1.2"
    assert r["similarity"] >= dd.TITLE_DICE_THRESHOLD


def test_find_similar_titles_skips_identical_titles():
    """完全同名是结构缺陷，由 duplicate_titles 负责，此处不得重复报告。"""
    nodes = [
        {"path": "1", "title": "基坑降水施工"},
        {"path": "2", "title": "基坑降水施工"},
    ]
    assert dd.find_similar_titles(nodes) == []


def test_find_similar_titles_skips_too_short_and_sorts_desc():
    nodes = [
        {"path": "1", "title": "AB"},                    # 归一化 < 3 → 跳过
        {"path": "2", "title": "基坑降水与支护施工"},
        {"path": "3", "title": "基坑降水及支护施工"},
        {"path": "4", "title": "质量验收标准"},
        {"path": "5", "title": "质量验收标准及程序"},
    ]
    res = dd.find_similar_titles(nodes)
    assert len(res) == 2
    assert res[0]["similarity"] >= res[1]["similarity"]
    assert all("AB" not in (r["a_title"], r["b_title"]) for r in res)


def test_find_similar_titles_tolerates_garbage():
    assert dd.find_similar_titles([None, "x", 1, {"title": ""}, {}]) == []
    assert dd.find_similar_titles(None) == []


# ---------------------------------------------------------------------------
# 7. CON-06 接线（规则注册 + 预检引擎）
# ---------------------------------------------------------------------------

def test_con06_registered_with_basis_and_program_mode():
    rule = get_rule("CON-06")
    assert rule is not None, "CON-06 必须登记进规则唯一事实源"
    assert rule.dimension == "consistency"
    assert rule.mode == "program", "查重必须是确定性程序规则（离线、秒级）"
    assert rule.basis, "缺行业依据 → 前端规则说明抽屉查不到、评分维度会塌陷"
    assert rule.title and rule.detail


def test_con06_registered_in_program_emitted_ids():
    """新增规则必须同步 _PROGRAM_EMITTED_RULE_IDS，否则注册表自检无法覆盖。"""
    assert "CON-06" in _PROGRAM_EMITTED_RULE_IDS
    assert validate_rule_registry(
        emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS) == []


def test_rule_version_bumped_for_con06():
    assert tuple(int(x) for x in RULE_VERSION.split(".")[:2]) >= (1, 7), (
        f"新增 CON-06 后必须 bump RULE_VERSION，当前 {RULE_VERSION}")


def test_con06_emitted_by_run_preflight():
    """端到端：run_preflight 必须真的产出 CON-06（不只是注册了规则）。"""
    dup = "基坑开挖深度为3m，边坡坡度1:0.75放坡，采用GB 50202-2018执行。"
    ctx = PreflightContext(
        scheme_id="x",
        sections=_sections(
            ("a", "施工工艺", dup + "本章节描述其它独有的施工工艺流程。"),
            ("b", "安全保证", dup + "本章节描述安全组织与职责分工的不同内容。"),
            ("c", "验收要求", _S2 + "验收内容清单完整并归档保存。"),
        ),
        charts=[],
    )
    findings = run_preflight(ctx)
    con06 = [f for f in findings if f["rule_id"].startswith("CON-06")]
    assert con06, "run_preflight 未产出 CON-06"
    f = con06[0]
    # 派生编号：避免 merge_findings 按 rule_id 去重时把多组搬运塌缩成一条
    assert f["rule_id"] != "CON-06"
    assert f["rule_id"] == "CON-06-1"
    assert f["dimension"] == "consistency"
    assert f["basis"], "CON-06-1 必须能回退解析到基规则的维度与依据"
    assert f["section_id"] in ("a", "b")
    assert f["section_title"]
    assert f["suggestion"]
    assert f["evidence"], "必须给出被搬运的文本片段，否则用户无从定位"


def test_con06_large_copy_escalates_to_high():
    big = _S1 + "其余部分继续扩写以满足大段判定的最小字数要求。"
    ctx = PreflightContext(
        sections=_sections(
            ("a", "施工工艺", big + "本章节描述其它独有的施工工艺流程。"),
            ("b", "安全保证", big + "本章节描述安全组织与职责分工的不同内容。"),
        ),
    )
    con06 = [f for f in run_preflight(ctx)
             if f["rule_id"].startswith("CON-06")]
    assert con06 and con06[0]["severity"] == "high", (
        "大段照抄必须升级为 high，否则评分无法体现评审硬伤")


def test_con05_and_con06_do_not_double_report():
    """核心防双报护栏：CON-05 已判定的章节对不得再出现在 CON-06 里。

    整章雷同时应只报 CON-05（整章级），不得同时报 CON-06（段落级）——
    否则同一现象被两条规则各扣一次分。
    注意 body 必须 ≥ DUP_MIN_WORDS(200)，否则 CON-05 根本不参与判定。
    """
    body = (_S1 + "。" + _S2 + "。" + _S1 + "。") * 3
    assert len(body) >= 200, "构造正文不足 DUP_MIN_WORDS，无法验证防双报"
    ctx = PreflightContext(
        sections=_sections(("a", "施工工艺", body), ("b", "安全保证", body)),
    )
    dup_pairs = [frozenset((d["a_id"], d["b_id"]))
                 for d in _find_duplicates(ctx.sections)]
    assert dup_pairs, "构造应触发 CON-05 的整章雷同判定"
    con06_titles = {f["section_title"] for f in run_preflight(ctx)
                    if f["rule_id"].startswith("CON-06")}
    for d in _find_duplicates(ctx.sections):
        assert frozenset((d["a_id"], d["b_id"])) in dup_pairs
        assert d["a_title"] not in con06_titles and \
            d["b_title"] not in con06_titles, (
                f"整章雷同章节对 {d['a_title']} / {d['b_title']} "
                f"被 CON-05 与 CON-06 双报")
    # 整章雷同应只走 CON-05，CON-06 必须为零
    assert not con06_titles


def test_check_duplication_tolerates_empty_context():
    assert check_duplication(PreflightContext(sections=[])) == []
    assert check_duplication(PreflightContext(sections=[{"id": "a"}])) == []


def test_no_duplication_finding_without_copy():
    """无搬运时必须零 CON-06（不能为了「显示有用」而凑数误报）。"""
    ctx = PreflightContext(
        sections=_sections(
            ("a", "施工工艺", "基坑开挖采用分层分段流水作业方式组织施工推进。"),
            ("b", "安全保证", "安全组织机构健全，特种作业人员持证上岗管理到位。"),
            ("c", "验收要求", "分项工程验收按现行规范逐条执行并留存书面记录。"),
        ),
    )
    con06 = [f for f in run_preflight(ctx)
             if f["rule_id"].startswith("CON-06")]
    assert con06 == [], f"无搬运内容却报出 CON-06：{[f['detail'] for f in con06]}"


# ---------------------------------------------------------------------------
# 8. 目录侧接线（加法式附加键）
# ---------------------------------------------------------------------------

def test_outline_continuity_has_similar_titles_key():
    outline = [
        {"id": "1", "title": "基坑降水与支护施工", "children": []},
        {"id": "2", "title": "基坑降水及支护施工", "children": []},
        {"id": "3", "title": "安全保证措施", "children": []},
    ]
    rep = check_outline_continuity(outline)
    assert "similar_titles" in rep
    assert len(rep["similar_titles"]) == 1
    r = rep["similar_titles"][0]
    assert {r["a_title"], r["b_title"]} == {
        "基坑降水与支护施工", "基坑降水及支护施工"}
    assert r["a_path"] == "1" and r["b_path"] == "2"


def test_similar_titles_does_not_affect_ok_contract():
    """加法式红线：近似标题不得改变 ok / issue_counts 的既有契约。

    近似标题是「建议明确区分」的组织建议，不是结构缺陷；
    若计入 issues 会让 ok 误变 False，进而误判目录不合格、误拦生成。
    """
    clean = [
        {"id": "1", "title": "基坑降水与支护施工", "children": []},
        {"id": "2", "title": "安全保证措施", "children": []},
    ]
    with_similar = [
        {"id": "1", "title": "基坑降水与支护施工", "children": []},
        {"id": "2", "title": "基坑降水及支护施工", "children": []},
        {"id": "3", "title": "安全保证措施", "children": []},
    ]
    rep_clean = check_outline_continuity(clean)
    rep_similar = check_outline_continuity(with_similar)
    assert rep_similar["ok"] == rep_clean["ok"] is True
    assert rep_similar["issue_counts"] == rep_clean["issue_counts"]
    assert "similar_titles" not in rep_similar["issue_counts"]


def test_similar_titles_empty_when_clean():
    outline = [
        {"id": "1", "title": "基坑降水与支护施工", "children": []},
        {"id": "2", "title": "安全保证措施", "children": []},
    ]
    assert check_outline_continuity(outline)["similar_titles"] == []


def test_outline_continuity_still_reports_exact_duplicates():
    """加法式改造后，同名章节仍必须由 duplicate_titles 报为缺陷。"""
    outline = [
        {"id": "1", "title": "基坑降水施工", "children": []},
        {"id": "2", "title": "基坑降水施工", "children": []},
    ]
    rep = check_outline_continuity(outline)
    assert rep["issue_counts"]["duplicate_titles"] == 1
    assert rep["ok"] is False
    assert rep["similar_titles"] == []   # 完全同名不进 similar_titles


# ---------------------------------------------------------------------------
# 9. 静态护栏（防止修复点被整段删掉 / 被后人改回分叉写法）
# ---------------------------------------------------------------------------

def test_split_never_treats_ascii_colon_as_terminator():
    """⚠️ 本轮实测复现的缺陷：``:`` 在施工文本里是**比值**（坡度 1:0.75）。

    把它当句末符会把「边坡坡度1:0.75放坡」切成两半，造出大量无意义短句。
    护栏用 AST 取正则字面量（纯文本匹配会被注释里的「:」假命中）。
    """
    tree = ast.parse(_src(DD_PATH))
    consts = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and "?" in n.value and "：" not in n.value
              and any(ch in n.value for ch in "。！？；")]
    assert consts, "未找到句子切分正则字面量"
    for value in consts:
        assert ":" not in value, (
            f"句子切分正则重新包含 ASCII 冒号：{value!r}（坡度 1:0.75 会被切开）")


def test_skeleton_applies_field_rules_before_stripping_punctuation():
    """⚠️ 本轮实测复现的缺陷：先剔除标点会让 ``95%`` 变成 ``95``，
    百分数规则必然落空、只能被裸数字规则兜底。"""
    fn = _fn(DD_PATH, "build_skeleton_key")
    i_norm = fn.find("normalize")
    i_rules = fn.find("_SKELETON_RULES")
    i_keep = fn.find("_keep_only")
    assert i_rules > 0 and i_keep > 0, "骨架键实现结构变化，需同步更新护栏"
    # 字段规则必须夹在 NFKC 折叠之后、标点清理之前
    assert i_norm < i_rules < i_keep, (
        "处理顺序必须是「NFKC 折叠 → 套字段规则 → 剔除标点」")


def test_skeleton_placeholder_braces_are_whitelisted_not_global():
    """占位符花括号只对骨架键放行，不得把 ``normalize_comparable`` 也改成保留
    （那会让「3m。」与「3m」不再等价，破坏相似度稳定性）。"""
    fn = _fn(DD_PATH, "normalize_comparable")
    assert "_SKELETON_KEEP_EXTRA" not in fn, (
        "normalize_comparable 不得引入占位符白名单")
    fn = _fn(DD_PATH, "build_skeleton_key")
    assert "extra=_SKELETON_KEEP_EXTRA" in fn


def test_cleanup_has_single_implementation():
    """``_keep_only`` 是唯一清理实现：两处不得各写一遍字符过滤。"""
    src = _src(DD_PATH)
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_keep_only" in funcs
    # 除 _keep_only 外不得再有 unicodedata.category 过滤
    hits = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and n.attr == "category"]
    # 允许 2 处：_keep_only 的实现 + (若有) 注释性引用
    assert len(hits) <= 2, f"出现多处 unicodedata.category 过滤（判据分叉）：{hits}"


def test_check_duplication_excludes_con05_pairs():
    """CON-06 的排除集合必须直接取自 CON-05 的判定，不得重写相似度口径。"""
    fn = _body(PE_PATH, "check_duplication")
    assert "_find_duplicates(ctx.sections)" in fn, (
        "CON-06 未复用 CON-05 的整章判定 → 两处相似度口径可能分叉")
    assert "exclude_pairs" in fn
    # 不得内联任何 Jaccard / 阈值常量（口径必须留在 CON-05 一侧）
    for forbidden in ("_jaccard", "_shingles", "DUP_SIMILARITY_THRESHOLD"):
        assert forbidden not in fn, f"check_duplication 内联了 {forbidden}"


def test_check_duplication_registered_in_runner():
    """⚠️ 漏进 ``run_preflight`` 的 checker 列表 = 规则注册了但永不执行。"""
    fn = _body(PE_PATH, "run_preflight")
    assert "check_duplication" in fn, (
        "check_duplication 未接入 run_preflight 的 checker 列表")


def test_check_duplication_fail_soft():
    """查重异常不得拖垮整轮预检（预检是「体检」，某一项失败仍要给结论）。"""
    fn = _body(PE_PATH, "check_duplication")
    assert "except Exception" in fn
    i = fn.find("except Exception")
    assert "exc_info=True" in fn[i:i + 200], "查重异常必须留堆栈，否则无法定位"


def test_con06_not_counted_in_ok_or_issue_counts():
    """⚠️ 加法式红线：similar_titles 不得混入 ``issues`` 字典。"""
    fn = _body(OQ_PATH, "check_outline_continuity")
    decl = fn[fn.find("issues: dict[str, list]"):fn.find("seen: dict")]
    assert decl.strip(), "未找到 issues 字典声明"
    assert "similar_titles" not in decl, (
        "similar_titles 混入 issues → ok 语义被改变、目录被误判不合格")
    assert '"similar_titles": similar_titles,' in fn
    # 确认它确实是顶层返回键（而不是 issues 内部键）
    ret = fn[fn.find("return {"):]
    assert '"similar_titles"' in ret


def test_audit_rules_con06_registered_in_consistency_group():
    """CON-06 必须登记在 consistency 维度组（否则评分维度塌陷）。"""
    src = _assigned(AR_PATH, "_CONSISTENCY_RULES")
    assert '"CON-06"' in src
    assert '"无跨章节段落搬运"' in src
    assert src.count("CHECK_MODE_PROGRAM") >= 2, "CON-05 / CON-06 均应为程序规则"


def test_inverted_index_candidate_generation():
    """倒排索引：候选必须共享足够 bigram，且遍历顺序确定（结论可复现）。"""
    flat = [
        ("a", "A", "\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b", dd.char_bigrams("\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b")),
        ("b", "B", "\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b", dd.char_bigrams("\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b")),
        ("c", "C", "\u5b50\u4e11\u5bc5\u536f\u8fb0\u5df3\u5348\u672a", dd.char_bigrams("\u5b50\u4e11\u5bc5\u536f\u8fb0\u5df3\u5348\u672a")),
        ("a", "A", "\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b\u58ec", dd.char_bigrams("\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b\u58ec")),
    ]
    inv = dd._build_bigram_inverted_index(flat)
    assert inv, "倒排索引为空"
    cands = dd._candidate_indices(flat[0][3], inv, 0, flat)
    # 只召回跨章节的完全同句 b；同章节的 a 必须被排除
    assert cands == [1], f"应只召回 b，跨章节与同章节过滤失效：{cands}"
    assert cands == sorted(cands), "候选必须升序（结论可复现）"


def test_high_df_gram_is_pruned():
    """高文档频率 gram 必须被跳过（否则候选生成退化为朴素两两）。

    这是实测复现的性能缺陷：施工文本高度模板化，高频 gram 的倒排表可达
    上万条，全量展开会让 4.8 万句的方案跑不完（实测 >30s 未完成）。
    """
    common = "\u5de5\u7a0b\u8981\u6c42"
    flat = [("s%d" % i, "t%d" % i,
             common + "\u7ec6\u8282%04d" % i,
             dd.char_bigrams(common + "\u7ec6\u8282%04d" % i))
            for i in range(50)]
    inv = dd._build_bigram_inverted_index(flat)
    inv[common[:2]] = list(range(50))          # 人为制造高频 gram
    cands = dd._candidate_indices(flat[0][3], inv, 0, flat)
    assert 1 in cands, "真实候选（靠低频大数字段）被误剪"
    assert len(cands) < 50, f"高频 gram 未被剪枝：{len(cands)} 候选"


def test_pairwise_pruning_is_lossless():
    """⚠️ 关键不变式：倒排剪枝**不得漏报**真实搬运。

    构造：两章共享一段**低频** bigram 充足的文本（真实照抄必然如此），
    同时全库塞满高频模板句制造噪声。剪枝后仍必须检出。
    """
    noise = "\u3002".join(
        "\u5de5\u7a0b\u8981\u6c42\u8fdb\u884c\u9a8c\u6536\u7ec8\u6e05%04d" % i
        for i in range(400))
    dup = ("\u57fa\u5751\u5143\u5751\u9762\u6e05\u9664\u4e0e\u79e9\u5ea6\u9a8c\u6536"
           "\u8981\u6c42\u5e94\u5728\u5f00\u63a7\u5751\u524d\u5b8c\u6210")
    secs = _sections(
        ("a", "\u65bd\u5de5\u5de5\u827a", dup + "\u3002\u672c\u7ae0\u4e13\u6709\u5185\u5bb9\u3002" + noise),
        ("b", "\u5b89\u5168\u4fdd\u8bc1", dup + "\u3002\u53e6\u7ae0\u4e13\u6709\u5185\u5bb9\u3002" + noise),
    )
    groups = dd.find_cross_section_copies(secs)["groups"]
    assert groups, "高频噪声淹没后仍必须检出真实搬运（剪枝过激）"


def test_all_changed_modules_parse():
    for path in (DD_PATH, PE_PATH, OQ_PATH, AR_PATH):
        ast.parse(_src(path), filename=path)


def test_no_replacement_character_in_changed_files():
    """防编码损坏（AGENTS.md §5.9：编辑工具曾把中文写成 U+FFFD）。"""
    for path in (DD_PATH, PE_PATH, OQ_PATH, AR_PATH):
        assert b"\xef\xbf\xbd" not in open(path, "rb").read(), \
            f"{path} 出现 U+FFFD 替换字符，中文已损坏"